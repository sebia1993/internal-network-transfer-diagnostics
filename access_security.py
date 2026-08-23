from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import stat
import threading
import time
from pathlib import Path

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)


ACCESS_TOKEN_ENV = "INTERNAL_TRANSFER_ACCESS_TOKEN"
ACCESS_TOKEN_FILE_DEFAULT = "data/.internal-transfer-access-token"
ACCESS_TOKEN_MIN_LENGTH = 32
ENROLLMENT_TOKEN_PREFIX = "enr_v1_"
SESSION_AUTH_KEY = "access_authenticated_at"
SESSION_CSRF_KEY = "csrf_token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class AccessSecurityError(RuntimeError):
    pass


def is_loopback_address(value: str | None) -> bool:
    raw = (value or "").strip()
    if raw.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return False


def _validate_access_token(token: str, *, source: str) -> str:
    if token != token.strip() or any(character.isspace() for character in token):
        raise AccessSecurityError(f"{source}에 공백이 포함되어 있습니다.")
    if len(token) < ACCESS_TOKEN_MIN_LENGTH:
        raise AccessSecurityError(
            f"{source}은(는) {ACCESS_TOKEN_MIN_LENGTH}자 이상이어야 합니다."
        )
    try:
        token.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AccessSecurityError(f"{source}은(는) ASCII 문자열이어야 합니다.") from exc
    return token


def _assert_private_token_file(path: Path) -> None:
    if path.is_symlink():
        raise AccessSecurityError("접근 토큰 파일은 심볼릭 링크일 수 없습니다.")
    if os.name != "nt":
        permissions = stat.S_IMODE(path.stat().st_mode)
        if permissions & 0o077:
            raise AccessSecurityError(
                "접근 토큰 파일 권한이 너무 넓습니다. 소유자만 읽고 쓸 수 있도록 0600으로 설정하세요."
            )


def load_or_create_access_token(
    app_root: Path,
    configured_path: str = ACCESS_TOKEN_FILE_DEFAULT,
    *,
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> tuple[str, Path | None]:
    environment = environ if environ is not None else os.environ
    environment_token = environment.get(ACCESS_TOKEN_ENV, "")
    if environment_token:
        return _validate_access_token(
            environment_token,
            source=f"환경 변수 {ACCESS_TOKEN_ENV}",
        ), None

    raw_path = Path(configured_path).expanduser()
    candidate_path = raw_path if raw_path.is_absolute() else app_root / raw_path
    try:
        if candidate_path.is_symlink():
            raise AccessSecurityError("접근 토큰 파일은 심볼릭 링크일 수 없습니다.")
        token_path = candidate_path.resolve(strict=False)
        token_path.parent.mkdir(parents=True, exist_ok=True)
    except AccessSecurityError:
        raise
    except (OSError, RuntimeError) as exc:
        raise AccessSecurityError("접근 토큰 파일 경로를 준비할 수 없습니다.") from exc

    if token_path.exists():
        try:
            _assert_private_token_file(token_path)
            token = token_path.read_text(encoding="ascii")
        except AccessSecurityError:
            raise
        except (OSError, UnicodeError) as exc:
            raise AccessSecurityError("접근 토큰 파일을 안전하게 읽을 수 없습니다.") from exc
        return _validate_access_token(token, source="접근 토큰 파일"), token_path

    token = f"itd_v1_{secrets.token_urlsafe(48)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(token_path, flags, 0o600)
        try:
            os.write(descriptor, token.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name != "nt":
            os.chmod(token_path, 0o600)
        _assert_private_token_file(token_path)
    except FileExistsError:
        return load_or_create_access_token(
            app_root,
            str(token_path),
            environ=environment,
        )
    except OSError as exc:
        raise AccessSecurityError("접근 토큰 파일을 안전하게 만들 수 없습니다.") from exc
    return token, token_path


class EnrollmentTokenStore:
    def __init__(
        self,
        *,
        ttl_seconds: int = 300,
        max_tokens: int = 256,
        clock=time.time,
    ) -> None:
        self.ttl_seconds = max(30, min(int(ttl_seconds), 3600))
        self.max_tokens = max(1, min(int(max_tokens), 4096))
        self.clock = clock
        self._lock = threading.Lock()
        self._digests: dict[str, float] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()

    def _prune_locked(self, now: float) -> None:
        expired = [digest for digest, expiry in self._digests.items() if expiry <= now]
        for digest in expired:
            self._digests.pop(digest, None)

    def issue(self) -> str:
        token = f"{ENROLLMENT_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        now = self.clock()
        with self._lock:
            self._prune_locked(now)
            while len(self._digests) >= self.max_tokens:
                oldest = min(self._digests, key=self._digests.get)  # type: ignore[arg-type]
                self._digests.pop(oldest, None)
            self._digests[self._digest(token)] = now + self.ttl_seconds
        return token

    def consume(self, token: str) -> bool:
        if not token.startswith(ENROLLMENT_TOKEN_PREFIX):
            return False
        now = self.clock()
        digest = self._digest(token)
        with self._lock:
            self._prune_locked(now)
            expiry = self._digests.pop(digest, None)
        return expiry is not None and expiry > now


class AccessSecurity:
    _AGENT_API_SUFFIXES = (
        "/connectivity-failure",
        "/jobs/next",
        "/control",
        "/complete",
    )

    def __init__(
        self,
        app: Flask,
        *,
        app_root: Path,
        token_file: str = ACCESS_TOKEN_FILE_DEFAULT,
        session_ttl_minutes: int = 480,
        enrollment_ttl_seconds: int = 300,
    ) -> None:
        self._master_token, self.token_path = load_or_create_access_token(
            app_root,
            token_file,
        )
        self.session_ttl_seconds = max(5, min(int(session_ttl_minutes), 24 * 60)) * 60
        self.enrollments = EnrollmentTokenStore(ttl_seconds=enrollment_ttl_seconds)
        signing_key = hmac.new(
            self._master_token.encode("ascii"),
            b"internal-transfer-flask-session-v1",
            hashlib.sha256,
        ).digest()
        app.secret_key = signing_key
        app.config.update(
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Strict",
            SESSION_COOKIE_SECURE=False,
            MAX_CONTENT_LENGTH=None,
        )
        self._install(app)

    def _bearer(self) -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        return header[7:].strip()

    def _master_bearer_valid(self) -> bool:
        candidate = self._bearer()
        return bool(candidate) and secrets.compare_digest(candidate, self._master_token)

    def _session_valid(self) -> bool:
        authenticated_at = session.get(SESSION_AUTH_KEY)
        if not isinstance(authenticated_at, (int, float)):
            return False
        if not 0 <= time.time() - float(authenticated_at) <= self.session_ttl_seconds:
            session.clear()
            return False
        return True

    def csrf_token(self) -> str:
        token = session.get(SESSION_CSRF_KEY)
        if not isinstance(token, str) or len(token) < 32:
            token = secrets.token_urlsafe(32)
            session[SESSION_CSRF_KEY] = token
        return token

    def _csrf_valid(self) -> bool:
        expected = session.get(SESSION_CSRF_KEY)
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token", "")
        return (
            isinstance(expected, str)
            and bool(supplied)
            and secrets.compare_digest(expected, supplied)
        )

    @staticmethod
    def _delegated_agent_api() -> bool:
        path = request.path
        if path == "/api/network-probe/agents/register":
            return True
        if not path.startswith("/api/network-probe/"):
            return False
        return any(path.endswith(suffix) for suffix in AccessSecurity._AGENT_API_SUFFIXES)

    @staticmethod
    def _unauthorized() -> Response | tuple[Response, int]:
        if request.path.startswith("/api/"):
            return jsonify({"error": "인증이 필요합니다."}), 401
        return redirect(url_for("access_login"), code=302)

    def require_agent_enrollment(self, client_ip: str) -> None:
        if is_loopback_address(client_ip):
            return
        token = self._bearer()
        if not token or not self.enrollments.consume(token):
            raise AccessSecurityError(
                "클라이언트 등록 토큰이 없거나 만료 또는 이미 사용되었습니다. 서버 화면에서 새 ZIP을 받으세요."
            )

    def issue_enrollment_token(self) -> str:
        return self.enrollments.issue()

    def _install(self, app: Flask) -> None:
        app.extensions["access_security"] = self

        @app.before_request
        def enforce_access_security():
            g.access_security_loopback = is_loopback_address(request.remote_addr)
            g.access_security_master_bearer = False
            if g.access_security_loopback:
                return None
            if request.endpoint in {"access_login", "static"}:
                return None
            if self._delegated_agent_api():
                return None
            if self._master_bearer_valid():
                g.access_security_master_bearer = True
                return None
            if not self._session_valid():
                return self._unauthorized()
            if request.method in UNSAFE_METHODS and not self._csrf_valid():
                return jsonify({"error": "CSRF 검증에 실패했습니다."}), 403
            return None

        @app.after_request
        def apply_security_headers(response: Response):
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Pragma", "no-cache")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'",
            )
            return response

        @app.route("/login", methods=["GET", "POST"])
        def access_login():
            if is_loopback_address(request.remote_addr):
                return redirect("/")
            if request.method == "GET":
                return render_template(
                    "login.html",
                    csrf_token=self.csrf_token(),
                    error="",
                )
            if not self._csrf_valid():
                return render_template(
                    "login.html",
                    csrf_token=self.csrf_token(),
                    error="로그인 요청 검증에 실패했습니다. 페이지를 새로 열어 다시 시도하세요.",
                ), 403
            candidate = request.form.get("access_token", "")
            if not candidate or not secrets.compare_digest(candidate, self._master_token):
                time.sleep(0.05)
                return render_template(
                    "login.html",
                    csrf_token=self.csrf_token(),
                    error="접근 토큰이 올바르지 않습니다.",
                ), 401
            session.clear()
            session[SESSION_AUTH_KEY] = time.time()
            session[SESSION_CSRF_KEY] = secrets.token_urlsafe(32)
            return redirect("/")

        @app.post("/logout")
        def access_logout():
            session.clear()
            return redirect(url_for("access_login"))

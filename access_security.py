from __future__ import annotations

import hashlib
import ipaddress
import secrets
import threading
import time
from pathlib import Path

from flask import Flask, Response, g, jsonify, redirect, request, session


# Legacy configuration compatibility only. The web access token is no longer
# created or validated, but app.py still accepts the old config key so existing
# config.ini files continue to start without migration.
ACCESS_TOKEN_FILE_DEFAULT = "data/.internal-transfer-access-token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ENROLLMENT_TOKEN_PREFIX = "enr_v1_"
SESSION_CSRF_KEY = "csrf_token"


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
    """Web CSRF/security headers plus TCP client enrollment protection.

    Web login/master-token authentication was intentionally removed. The
    constructor keeps the legacy token/session arguments only so existing
    config files and app wiring remain backward compatible.
    """

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
        # These values are intentionally ignored after removal of web login
        # authentication. Keeping the parameters avoids forcing a config
        # migration on existing installations.
        del app_root, token_file, session_ttl_minutes
        self.token_path: Path | None = None
        self.enrollments = EnrollmentTokenStore(ttl_seconds=enrollment_ttl_seconds)

        # CSRF still needs a session signing key, but it is now independent of
        # any operator-entered token and exists only for the current process.
        app.secret_key = secrets.token_bytes(32)
        app.config.update(
            SESSION_COOKIE_HTTPONLY=True,
            SESSION_COOKIE_SAMESITE="Strict",
            SESSION_COOKIE_SECURE=False,
            MAX_CONTENT_LENGTH=None,
        )
        self._install(app)

    @staticmethod
    def _bearer() -> str:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return ""
        return header[7:].strip()

    def csrf_token(self) -> str:
        token = session.get(SESSION_CSRF_KEY)
        if not isinstance(token, str) or len(token) < 32:
            token = secrets.token_urlsafe(32)
            session[SESSION_CSRF_KEY] = token
        return token

    def _csrf_valid(self) -> bool:
        expected = session.get(SESSION_CSRF_KEY)
        supplied = request.headers.get("X-CSRF-Token") or request.form.get(
            "_csrf_token",
            "",
        )
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
            # Kept for compatibility with any diagnostics that may inspect it.
            # Master bearer authentication itself no longer exists.
            g.access_security_master_bearer = False

            if g.access_security_loopback:
                return None
            if request.endpoint == "static":
                return None
            if self._delegated_agent_api():
                return None
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

        # Compatibility redirects for old bookmarks/UI. Neither route performs
        # authentication or asks for an access token.
        @app.get("/login")
        def access_login():
            return redirect("/")

        @app.post("/logout")
        def access_logout():
            session.clear()
            return redirect("/")

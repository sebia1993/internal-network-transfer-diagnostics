from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


GENERATED_NAMES = {
    "SECURITY_REVIEW_KO.md",
    "security_manifest.json",
    "sbom.cdx.json",
    "SHA256SUMS.txt",
}
LOCK_ENTRY_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\s]+)\s+--hash=sha256:(?P<sha256>[0-9a-fA-F]{64})$"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_text_lf(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    with open(path, "w", encoding=encoding, newline="\n") as handle:
        handle.write(content)


def package_files(root: Path, *, exclude_generated: bool) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if exclude_generated and path.name in GENERATED_NAMES and path.parent == root:
            continue
        files.append(path)
    return files


def locked_components(lock_path: Path) -> list[dict[str, object]]:
    components = []
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Windows dependency lock could not be read: {lock_path}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_ENTRY_RE.fullmatch(line)
        if not match:
            raise ValueError(f"Invalid Windows dependency lock entry at line {line_number}")
        name = match.group("name")
        version = match.group("version")
        normalized = name.replace("_", "-").lower()
        components.append(
            {
                "type": "library",
                "name": name,
                "version": version,
                "purl": f"pkg:pypi/{normalized}@{version}",
                "hashes": [{"alg": "SHA-256", "content": match.group("sha256").lower()}],
            }
        )
    return sorted(components, key=lambda item: (str(item["name"]).casefold(), str(item["version"])))


def generate_security_artifacts(
    root: Path,
    *,
    version: str,
    source_commit: str,
    lock_path: Path | None = None,
) -> None:
    root = root.resolve()
    dependency_lock = (lock_path or Path(__file__).resolve().parents[1] / "requirements-windows.lock").resolve()
    built_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    files = [
        {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in package_files(root, exclude_generated=True)
    ]
    manifest = {
        "schema_version": 1,
        "product": "Internal Upload and Network Check",
        "version": version,
        "source_commit": source_commit,
        "built_at_utc": built_at,
        "signed": False,
        "expected_behavior": {
            "server_listeners": ["TCP 8000 by default for HTTP", "TCP 5201 by default for TCP measurement"],
            "client_network": [
                "HTTP long polling to the configured server only",
                "TCP measurement traffic to the port returned by that server only",
            ],
            "child_processes": ["start_internal_upload.cmd launches InternalUploadServer.exe"],
            "not_implemented": [
                "persistence or automatic startup",
                "privilege elevation",
                "automatic firewall modification",
                "uploaded file execution",
                "external internet command and control",
            ],
        },
        "files": files,
    }
    write_text_lf(
        root / "security_manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )

    sbom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": built_at,
            "component": {
                "type": "application",
                "name": "Internal Upload and Network Check",
                "version": version,
            },
            "properties": [
                {"name": "release:dependency-lock-sha256", "value": sha256_file(dependency_lock)},
            ],
        },
        "components": locked_components(dependency_lock),
    }
    write_text_lf(
        root / "sbom.cdx.json",
        json.dumps(sbom, ensure_ascii=False, indent=2) + "\n",
    )

    review = f"""# 보안 검토 정보

- 제품: 사내 업로드 및 네트워크 체크
- 버전: {version}
- 소스 커밋: {source_commit}
- 빌드 시각(UTC): {built_at}
- 코드서명: 미적용

## 예상 실행 동작

- 서버는 기본적으로 HTTP TCP 8000과 TCP 측정 5201을 수신합니다.
- TCP 클라이언트는 설정된 서버 한 곳에만 HTTP로 작업을 조회하고 해당 서버가 알려 준 TCP 포트로 측정합니다.
- 서버 시작 과정에서 PowerShell을 실행하지 않으며 방화벽을 자동 변경하지 않습니다.
- 업로드된 파일을 실행하지 않고 레지스트리, 시작프로그램, 예약 작업을 만들지 않습니다.
- 외부 인터넷 서버에 측정 결과나 업로드 파일을 전송하지 않습니다.

## 운영상 남는 위험

- 루프백이 아닌 웹 접근은 토큰 로그인 또는 Bearer 인증을 요구하며 브라우저 상태 변경 요청은 CSRF를 검증합니다.
- TCP 제어 프레임은 HMAC-SHA256, timestamp와 nonce를 검증하고 Windows 클라이언트 등록 토큰은 짧은 시간 동안 한 번만 사용할 수 있습니다.
- 내장 HTTP/TCP 전송은 암호화하지 않으므로 신뢰할 수 있는 내부망·VPN 또는 TLS 역방향 프록시가 필요합니다.
- 업로드 파일 크기 제한과 압축파일 내부 검사는 적용하지 않습니다.
- TCP 클라이언트는 웹 측정을 기다리는 동안 서버에 장기 폴링합니다.
- 실행파일, 스크립트, 매크로 문서와 디스크 이미지는 직접 업로드할 수 없습니다.

파일별 해시는 `security_manifest.json`과 `SHA256SUMS.txt`, 의존성은 `sbom.cdx.json`을 확인하세요.
"""
    write_text_lf(root / "SECURITY_REVIEW_KO.md", review)

    checksum_lines = [
        f"{sha256_file(path)}  {path.relative_to(root).as_posix()}"
        for path in package_files(root, exclude_generated=False)
        if path.name != "SHA256SUMS.txt"
    ]
    write_text_lf(root / "SHA256SUMS.txt", "\n".join(checksum_lines) + "\n", encoding="ascii")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--requirements-lock", default="")
    args = parser.parse_args(argv)
    generate_security_artifacts(
        Path(args.root),
        version=args.version,
        source_commit=args.source_commit,
        lock_path=Path(args.requirements_lock) if args.requirements_lock else None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

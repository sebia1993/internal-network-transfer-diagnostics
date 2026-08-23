from __future__ import annotations

import re
import subprocess
from pathlib import Path


MAX_SCAN_BYTES = 2 * 1024 * 1024
TOKEN_FILENAME = ".internal-transfer-access-token"
SECRET_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "application-access-token": re.compile(rb"itd_v1_[A-Za-z0-9_-]{32,}"),
}


def tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return [root / value.decode("utf-8") for value in result.stdout.split(b"\0") if value]


def scan_paths(paths: list[Path]) -> list[tuple[str, int, str]]:
    findings: list[tuple[str, int, str]] = []
    common_root = Path.cwd().resolve()
    for path in paths:
        resolved = path.resolve()
        display = (
            resolved.relative_to(common_root).as_posix()
            if resolved.is_relative_to(common_root)
            else resolved.name
        )
        if path.name == TOKEN_FILENAME:
            findings.append((display, 0, "runtime-token-file"))
            continue
        try:
            if not path.is_file() or path.stat().st_size > MAX_SCAN_BYTES:
                continue
            payload = path.read_bytes()
        except OSError:
            findings.append((display, 0, "unreadable-tracked-file"))
            continue
        for rule, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(payload):
                line = payload.count(b"\n", 0, match.start()) + 1
                findings.append((display, line, rule))
    return findings


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings = scan_paths(tracked_paths(root))
    if findings:
        for path, line, rule in findings:
            location = f"{path}:{line}" if line else path
            print(f"secret scan finding: {location} rule={rule}")
        return 1
    print("Tracked secret scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

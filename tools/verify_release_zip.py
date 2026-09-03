from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import stat
import sys
from configparser import ConfigParser, Error as ConfigParserError
from pathlib import PurePosixPath
from zipfile import ZipFile, ZipInfo


SERVER_EXECUTABLE = "InternalUploadServer.exe"
CLIENT_EXECUTABLE = "client-template/NetworkProbeClient.exe"
REQUIRED_FILES = {
    SERVER_EXECUTABLE,
    CLIENT_EXECUTABLE,
    "start_internal_upload.cmd",
    "config.ini",
    "README_START_HERE_KO.txt",
    "README.md",
    "RELEASE_NOTES.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY_REVIEW_KO.md",
    "security_manifest.json",
    "sbom.cdx.json",
    "SHA256SUMS.txt",
    "data/upload_log.csv",
    "data/network_check_log.csv",
    "data/network_check_session_log.csv",
    "data/network_check_results/README_RESULTS_KO.txt",
    "data/network_probe_log.csv",
    "data/network_probe_results/README_RESULTS_KO.txt",
    "uploads/README_UPLOADS_KO.txt",
}
FORBIDDEN_PARTS = {
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "tests",
    "tools",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".spec"}
GENERATED_SECURITY_FILES = {
    "SECURITY_REVIEW_KO.md",
    "security_manifest.json",
    "sbom.cdx.json",
    "SHA256SUMS.txt",
}
ALLOWED_DYNAMIC_PREFIXES = {"_internal/", "client-template/_internal/"}


def validate_archive_entries(entries: list[ZipInfo]) -> list[str]:
    errors = []
    exact_names: set[str] = set()
    windows_names: set[str] = set()
    for entry in entries:
        raw_name = entry.filename
        normalized = raw_name.replace("\\", "/")
        if not normalized:
            continue
        candidate = normalized.rstrip("/")
        path = PurePosixPath(candidate)
        unsafe = (
            normalized.startswith("/")
            or bool(re.match(r"^[A-Za-z]:", candidate))
            or ".." in path.parts
            or ":" in candidate
            or any(ord(character) < 32 for character in candidate)
            or any(part.endswith((" ", ".")) for part in path.parts)
        )
        if unsafe:
            errors.append(f"unsafe path in ZIP: {raw_name}")
            continue
        unix_mode = (entry.external_attr >> 16) & 0o170000
        if stat.S_ISLNK(unix_mode) or entry.flag_bits & 0x1:
            errors.append(f"unsupported link or encrypted entry in ZIP: {raw_name}")
            continue
        if normalized.endswith("/"):
            continue
        if path.name == ".internal-transfer-access-token":
            errors.append(f"runtime access token file must not be packaged: {raw_name}")
            continue
        if normalized in exact_names or normalized.casefold() in windows_names:
            errors.append(f"duplicate Windows path in ZIP: {raw_name}")
            continue
        exact_names.add(normalized)
        windows_names.add(normalized.casefold())
    return errors


def normalized_names(zip_file: ZipFile) -> set[str]:
    names = set()
    for name in zip_file.namelist():
        normalized = name.replace("\\", "/")
        if normalized and not normalized.endswith("/"):
            names.add(normalized)
    return names


def validate_no_forbidden_entries(names: set[str]) -> list[str]:
    errors = []
    for name in sorted(names):
        path = PurePosixPath(name)
        parts = set(path.parts)
        if parts & FORBIDDEN_PARTS:
            errors.append(f"forbidden path in ZIP: {name}")
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            errors.append(f"forbidden file suffix in ZIP: {name}")
    return errors


def validate_release_layout(names: set[str]) -> list[str]:
    errors = []
    for name in sorted(names):
        if PurePosixPath(name).suffix.casefold() == ".exe" and name not in {SERVER_EXECUTABLE, CLIENT_EXECUTABLE}:
            errors.append(f"unexpected executable in release ZIP: {name}")
        elif name not in REQUIRED_FILES and not any(name.startswith(prefix) for prefix in ALLOWED_DYNAMIC_PREFIXES):
            errors.append(f"unexpected file in release ZIP: {name}")
    return errors


def validate_csv_header(zip_file: ZipFile, name: str, expected_prefix: list[str]) -> list[str]:
    with zip_file.open(name) as handle:
        text = handle.read().decode("utf-8-sig")
    rows = list(csv.reader(text.splitlines()))
    if not rows or rows[0][: len(expected_prefix)] != expected_prefix:
        return [f"{name} has an unexpected header"]
    if len(rows) != 1:
        return [f"{name} must contain only the initial header row"]
    return []


def validate_server_launcher(zip_file: ZipFile) -> list[str]:
    errors = []
    raw_command = zip_file.read("start_internal_upload.cmd")
    if raw_command.startswith(b"\xef\xbb\xbf"):
        errors.append("start_internal_upload.cmd must not start with a UTF-8 BOM")
    try:
        command = raw_command.decode("utf-8")
    except UnicodeDecodeError:
        return errors + ["start_internal_upload.cmd must be valid UTF-8"]
    if not command.startswith("@echo off"):
        errors.append("start_internal_upload.cmd must start with @echo off")
    if SERVER_EXECUTABLE not in command:
        errors.append(f"start_internal_upload.cmd does not start {SERVER_EXECUTABLE}")
    if "실제 접속 주소" not in command or "config.ini" not in command:
        errors.append("start_internal_upload.cmd does not explain automatic web port selection")
    if "powershell" in command.casefold() or "executionpolicy" in command.casefold():
        errors.append("start_internal_upload.cmd must not invoke PowerShell")
    return errors


def validate_client_template(names: set[str]) -> list[str]:
    errors = []
    if not any(name.startswith("_internal/") for name in names):
        errors.append("server onedir runtime folder is missing")
    if not any(name.startswith("client-template/_internal/") for name in names):
        errors.append("client onedir runtime folder is missing")
    for name in sorted(names):
        lowered = name.casefold()
        if not lowered.startswith("client-template/"):
            continue
        if lowered.endswith(".cmd") or lowered.endswith("config.ini"):
            errors.append(f"client template contains a launcher or server config: {name}")
        if lowered.endswith("internaluploadserver.exe"):
            errors.append(f"client template contains the server executable: {name}")
    if "InternalUpload.exe" in names or "start_tcp_probe_client.cmd" in names:
        errors.append("legacy combined server/client files remain in the release")
    return errors


def validate_default_config(zip_file: ZipFile) -> list[str]:
    parser = ConfigParser()
    try:
        parser.read_string(zip_file.read("config.ini").decode("utf-8-sig"))
        if parser.getint("app", "CONFIG_VERSION", fallback=0) < 3:
            return ["config.ini must use CONFIG_VERSION=3 or newer"]
        if not parser.getboolean("network_probe", "ENABLED", fallback=False):
            return ["config.ini must enable TCP probe by default"]
        if not 1 <= parser.getint("network_probe", "PORT", fallback=0) <= 65535:
            return ["config.ini has an invalid TCP probe port"]
        if not 30 <= parser.getint(
            "security",
            "ENROLLMENT_TOKEN_TTL_SECONDS",
            fallback=0,
        ) <= 3600:
            return ["config.ini has an invalid enrollment token TTL"]
    except (ConfigParserError, KeyError, ValueError):
        return ["config.ini has invalid default settings"]
    return []


def validate_security_artifacts(zip_file: ZipFile, names: set[str], version: str | None) -> list[str]:
    errors = []
    try:
        manifest = json.loads(zip_file.read("security_manifest.json").decode("utf-8"))
        sbom = json.loads(zip_file.read("sbom.cdx.json").decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"security artifact is invalid: {exc}"]

    if not isinstance(manifest, dict) or not isinstance(sbom, dict):
        return ["security artifact root must be a JSON object"]

    if manifest.get("schema_version") != 1 or manifest.get("signed") is not False:
        errors.append("security_manifest.json has invalid trust metadata")
    if version and manifest.get("version") != version:
        errors.append("security_manifest.json version does not match release version")
    if not re.fullmatch(r"[0-9a-fA-F]{40}", str(manifest.get("source_commit", ""))):
        errors.append("security_manifest.json source commit must be a full Git SHA")
    manifest_items = manifest.get("files")
    manifest_files: dict[str, dict[str, object]] = {}
    if not isinstance(manifest_items, list):
        errors.append("security_manifest.json files must be a list")
    else:
        for item in manifest_items:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                errors.append("security_manifest.json has an invalid file entry")
                continue
            name = str(item["path"])
            if name in manifest_files:
                errors.append(f"security_manifest.json has a duplicate file: {name}")
                continue
            manifest_files[name] = item

        expected_manifest_names = names - GENERATED_SECURITY_FILES
        if set(manifest_files) != expected_manifest_names:
            errors.append("security_manifest.json file list does not match release contents")
        for name, item in manifest_files.items():
            if name not in names:
                continue
            content = zip_file.read(name)
            if item.get("size") != len(content) or item.get("sha256") != hashlib.sha256(content).hexdigest():
                errors.append(f"security manifest hash mismatch: {name}")

    if sbom.get("bomFormat") != "CycloneDX" or not isinstance(sbom.get("components"), list):
        errors.append("sbom.cdx.json is not a CycloneDX component list")
    else:
        components = {
            str(item.get("name", "")).casefold(): str(item.get("version", ""))
            for item in sbom["components"]
            if isinstance(item, dict)
        }
        for dependency in ("flask", "openpyxl", "pyinstaller"):
            if not components.get(dependency):
                errors.append(f"sbom.cdx.json is missing release dependency: {dependency}")

    checksum_entries: dict[str, str] = {}
    try:
        for line in zip_file.read("SHA256SUMS.txt").decode("ascii").splitlines():
            digest, name = line.split("  ", 1)
            if name in checksum_entries or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("duplicate name or invalid digest")
            checksum_entries[name] = digest
    except (KeyError, UnicodeDecodeError, ValueError):
        errors.append("SHA256SUMS.txt has an invalid format")
        return errors
    expected_checksum_names = names - {"SHA256SUMS.txt"}
    if set(checksum_entries) != expected_checksum_names:
        errors.append("SHA256SUMS.txt file list does not match release contents")
    for name, digest in checksum_entries.items():
        if name not in names or digest != hashlib.sha256(zip_file.read(name)).hexdigest():
            errors.append(f"SHA256SUMS.txt hash mismatch: {name}")
    return errors


def verify_zip(zip_path: str, version: str | None = None) -> list[str]:
    errors = []
    with ZipFile(zip_path) as archive:
        errors.extend(validate_archive_entries(archive.infolist()))
        names = normalized_names(archive)
        missing = sorted(REQUIRED_FILES - names)
        errors.extend(f"missing required file: {name}" for name in missing)
        errors.extend(validate_no_forbidden_entries(names))
        errors.extend(validate_release_layout(names))
        errors.extend(validate_client_template(names))
        operational_results = sorted(
            name for name in names if name.startswith("data/network_check_results/") and name.lower().endswith(".json")
        )
        errors.extend(f"operational result in ZIP: {name}" for name in operational_results)
        operational_probe_results = sorted(
            name for name in names if name.startswith("data/network_probe_results/") and name.lower().endswith(".json")
        )
        errors.extend(f"operational probe result in ZIP: {name}" for name in operational_probe_results)
        csv_checks = [
            ("data/upload_log.csv", ["upload_id", "uploaded_at", "original_filename"]),
            ("data/network_check_log.csv", ["checked_at", "client_ip", "direction"]),
            ("data/network_check_session_log.csv", ["checked_at", "session_id", "client_ip", "direction"]),
            (
                "data/network_probe_log.csv",
                ["checked_at", "session_id", "agent_id", "agent_hostname", "client_ip", "server_host"],
            ),
        ]
        for name, header in csv_checks:
            if name in names:
                errors.extend(validate_csv_header(archive, name, header))
        if version and "README_START_HERE_KO.txt" in names:
            readme = archive.read("README_START_HERE_KO.txt").decode("utf-8-sig")
            if version not in readme:
                errors.append(f"README_START_HERE_KO.txt does not mention {version}")
        if "start_internal_upload.cmd" in names:
            errors.extend(validate_server_launcher(archive))
        if "config.ini" in names:
            errors.extend(validate_default_config(archive))
        if {"security_manifest.json", "sbom.cdx.json", "SHA256SUMS.txt"}.issubset(names):
            errors.extend(validate_security_artifacts(archive, names, version))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, dest="zip_path")
    parser.add_argument("--version", default="")
    args = parser.parse_args(argv)

    errors = verify_zip(args.zip_path, args.version or None)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("Release ZIP verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

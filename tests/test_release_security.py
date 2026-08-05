from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.generate_security_artifacts import generate_security_artifacts
from tools.generate_windows_version_info import build_version_info


def test_windows_version_info_identifies_separate_server_and_client():
    value = build_version_info(
        "v0.4.4-rc.1",
        product_name="Internal Upload Server",
        description="Internal file upload server",
        filename="InternalUploadServer.exe",
    )

    assert "filevers=(0, 4, 4, 1)" in value
    assert "InternalUploadServer.exe" in value
    assert "Internal file upload server" in value


def test_windows_release_build_requires_clean_worktree_and_onedir_bundles():
    script = (Path(__file__).resolve().parents[1] / "tools" / "build_windows_release.ps1").read_text(
        encoding="utf-8"
    )

    assert "git status --porcelain" in script
    assert "--onedir" in script
    assert "--onefile" not in script
    assert "InternalUploadServer.exe" in script
    assert "NetworkProbeClient.exe" in script
    assert "powershell.exe" not in script.casefold()
    assert "v0.5.1`` tag workflow" in script
    assert "``persistence_complete``" in script
    assert "gate 해제와 완료 플래그 공개를 같은 임계구역" in script


def test_security_artifacts_record_commit_roles_and_file_hashes(tmp_path):
    server = tmp_path / "InternalUploadServer.exe"
    client = tmp_path / "client-template" / "NetworkProbeClient.exe"
    client.parent.mkdir()
    server.write_bytes(b"server")
    client.write_bytes(b"client")

    generate_security_artifacts(tmp_path, version="v0.4.4-rc.1", source_commit="b" * 40)

    manifest = json.loads((tmp_path / "security_manifest.json").read_text(encoding="utf-8"))
    files = {item["path"]: item["sha256"] for item in manifest["files"]}
    assert manifest["source_commit"] == "b" * 40
    assert manifest["signed"] is False
    assert files["InternalUploadServer.exe"] == hashlib.sha256(b"server").hexdigest()
    assert files["client-template/NetworkProbeClient.exe"] == hashlib.sha256(b"client").hexdigest()
    assert "PowerShell" in (tmp_path / "SECURITY_REVIEW_KO.md").read_text(encoding="utf-8")
    assert "security_manifest.json" in (tmp_path / "SHA256SUMS.txt").read_text(encoding="ascii")
    sbom = json.loads((tmp_path / "sbom.cdx.json").read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    components = {item["name"]: item for item in sbom["components"]}
    assert components["flask"]["version"] == "3.1.3"
    assert len(components["flask"]["hashes"][0]["content"]) == 64
    assert "macholib" not in components
    assert "exceptiongroup" not in components
    assert any(prop["name"] == "release:dependency-lock-sha256" for prop in sbom["metadata"]["properties"])

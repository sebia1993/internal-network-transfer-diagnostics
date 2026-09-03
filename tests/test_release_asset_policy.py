from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_github_release_publishes_only_windows_zip_asset():
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    expected_zip = '$zip = "dist/internal-upload_${env:RELEASE_VERSION}_windows.zip"'
    create_command = "gh release create $env:RELEASE_VERSION $zip --repo"

    assert expected_zip in workflow
    assert workflow.count(create_command) == 2
    assert "gh release create $env:RELEASE_VERSION $zip $sha" not in workflow
    assert "gh release create $env:RELEASE_VERSION $zip $sbom" not in workflow
    assert "--json assets --jq '.assets[].name'" in workflow
    assert "$assets.Count -ne 1" in workflow
    assert "$assets[0] -ne $expectedAsset" in workflow

from tools.scan_tracked_secrets import scan_paths


def test_secret_scan_reports_rule_and_location_without_echoing_secret(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    secret = "itd_v1_" + ("A" * 40)
    path = tmp_path / "sample.txt"
    path.write_text(f"safe\n{secret}\n", encoding="utf-8")
    findings = scan_paths([path])
    assert findings == [("sample.txt", 2, "application-access-token")]
    assert secret not in repr(findings)


def test_runtime_token_filename_is_rejected_even_when_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".internal-transfer-access-token"
    path.write_text("", encoding="ascii")
    assert scan_paths([path]) == [
        (".internal-transfer-access-token", 0, "runtime-token-file")
    ]

from __future__ import annotations

import io
import json
import hashlib
from zipfile import ZipFile

import pytest

from app_version import APP_VERSION
import network_probe.client_package as client_package_module
from network_probe.client_package import (
    ClientPackageError,
    build_client_package,
    resolve_client_server_url,
    verify_client_package,
)


@pytest.mark.parametrize(
    ("request_host", "expected"),
    [
        ("192.168.10.25:8123", "http://192.168.10.25:8123"),
        ("SERVER-PC:9000", "http://server-pc:9000"),
        ("fileserver.local", "http://fileserver.local:80"),
    ],
)
def test_resolve_client_server_url_uses_current_request_host(request_host, expected):
    assert (
        resolve_client_server_url(
            request_host,
            fallback_host="192.168.10.99",
            fallback_port=8000,
        )
        == expected
    )


@pytest.mark.parametrize("request_host", ["localhost:9999", "127.0.0.1:9999", "127.10.20.30"])
def test_resolve_client_server_url_replaces_loopback_with_lan_ipv4(request_host):
    assert (
        resolve_client_server_url(
            request_host,
            fallback_host="10.20.30.40",
            fallback_port=8123,
        )
        == "http://10.20.30.40:8123"
    )


@pytest.mark.parametrize(
    "request_host",
    [
        "",
        "user@server-pc:8000",
        "server-pc/path:8000",
        "server-pc%COMSPEC%:8000",
        "server-pc&calc:8000",
        "server-pc|calc:8000",
        "[::1]:8000",
        "999.999.999.999:8000",
        "server-pc:0",
        "server-pc:65536",
    ],
)
def test_resolve_client_server_url_rejects_unsafe_or_unsupported_hosts(request_host):
    with pytest.raises(ClientPackageError):
        resolve_client_server_url(
            request_host,
            fallback_host="10.20.30.40",
            fallback_port=8000,
        )


def test_resolve_client_server_url_rejects_loopback_fallback():
    with pytest.raises(ClientPackageError, match="사내 IPv4"):
        resolve_client_server_url(
            "localhost:8000",
            fallback_host="127.0.0.1",
            fallback_port=8000,
        )


def make_client_bundle(tmp_path):
    bundle = tmp_path / "client-template"
    bundle.mkdir()
    executable = bundle / "NetworkProbeClient.exe"
    executable.write_bytes(b"MZ-client-test")
    internal = bundle / "_internal"
    internal.mkdir()
    (internal / "python-runtime.dll").write_bytes(b"runtime")
    return bundle


def test_build_client_package_contains_only_autoconnect_client_files(tmp_path):
    bundle = make_client_bundle(tmp_path)
    server_url = "http://server-pc:8000"

    enrollment_token = "enr_v1_" + ("x" * 44)
    package = build_client_package(bundle, server_url, enrollment_token=enrollment_token)

    assert package.download_name == "internal-upload-client_server-pc.zip"
    assert package.root_name == "InternalUpload_Client_server-pc"
    assert verify_client_package(package.payload, server_url) == []
    with ZipFile(io.BytesIO(package.payload)) as archive:
        assert set(archive.namelist()) == {
            "InternalUpload_Client_server-pc/NetworkProbeClient.exe",
            "InternalUpload_Client_server-pc/_internal/python-runtime.dll",
            "InternalUpload_Client_server-pc/client-config.json",
            "InternalUpload_Client_server-pc/client-manifest.json",
            "InternalUpload_Client_server-pc/README_CLIENT_KO.txt",
        }
        executable = archive.read("InternalUpload_Client_server-pc/NetworkProbeClient.exe")
        config = json.loads(
            archive.read("InternalUpload_Client_server-pc/client-config.json").decode("utf-8")
        )
        manifest = json.loads(
            archive.read("InternalUpload_Client_server-pc/client-manifest.json").decode("utf-8")
        )
        readme = archive.read("InternalUpload_Client_server-pc/README_CLIENT_KO.txt").decode(
            "utf-8-sig"
        )
    assert executable == b"MZ-client-test"
    assert config == {
        "schema_version": 2,
        "server_url": server_url,
        "client_version": APP_VERSION,
        "enrollment_token": enrollment_token,
    }
    assert manifest["executable"] == "NetworkProbeClient.exe"
    assert manifest["executable_sha256"] == hashlib.sha256(executable).hexdigest()
    manifest_files = {item["path"]: item for item in manifest["files"]}
    assert set(manifest_files) == {
        "NetworkProbeClient.exe",
        "_internal/python-runtime.dll",
        "client-config.json",
        "README_CLIENT_KO.txt",
    }
    assert manifest_files["_internal/python-runtime.dll"]["sha256"] == hashlib.sha256(b"runtime").hexdigest()
    assert package.client_executable_sha256 == manifest["executable_sha256"]
    assert server_url in readme
    assert APP_VERSION in readme
    assert "TCP 전송 성능 측정" in readme
    assert "config.ini" in readme
    assert "TCP 측정 포트는 서버가 자동으로 전달" in readme
    assert "한 번만" in readme
    assert "InternalUploadServer.exe" not in readme


def test_verify_client_package_detects_modified_runtime_file(tmp_path):
    bundle = make_client_bundle(tmp_path)
    server_url = "http://server-pc:8000"
    package = build_client_package(bundle, server_url)
    source = io.BytesIO(package.payload)
    output = io.BytesIO()
    with ZipFile(source) as original, ZipFile(output, "w") as modified:
        for item in original.infolist():
            content = original.read(item.filename)
            if item.filename.endswith("_internal/python-runtime.dll"):
                content = b"modified"
            modified.writestr(item, content)

    assert any("해시" in error for error in verify_client_package(output.getvalue(), server_url))


def test_build_client_package_rejects_missing_bundle(tmp_path):
    with pytest.raises(ClientPackageError, match="프로그램 폴더"):
        build_client_package(tmp_path / "missing", "http://server-pc:8000")


@pytest.mark.parametrize(
    "forbidden_name",
    ["start.cmd", "config.ini", "InternalUploadServer.exe", "unexpected.txt"],
)
def test_build_client_package_rejects_server_or_launcher_files(tmp_path, forbidden_name):
    bundle = make_client_bundle(tmp_path)
    (bundle / forbidden_name).write_bytes(b"forbidden")

    with pytest.raises(ClientPackageError, match="허용되지 않는|서버 실행|예상하지 않은"):
        build_client_package(bundle, "http://server-pc:8000")


def test_build_client_package_rejects_additional_internal_executable(tmp_path):
    bundle = make_client_bundle(tmp_path)
    (bundle / "_internal" / "helper.exe").write_bytes(b"MZ-helper")

    with pytest.raises(ClientPackageError, match="추가 실행 파일"):
        build_client_package(bundle, "http://server-pc:8000")


def test_client_package_zip_io_failures_do_not_expose_raw_exception(
    tmp_path,
    monkeypatch,
):
    bundle = make_client_bundle(tmp_path)

    def fail_zip(*args, **kwargs):
        raise OSError(r"C:\private-customer\zip-detail")

    monkeypatch.setattr(client_package_module, "ZipFile", fail_zip)

    errors = verify_client_package(b"not-used", "http://server-pc:8000")
    assert errors == [
        "클라이언트 ZIP을 확인할 수 없습니다. 오류 코드: "
        "CLIENT_PACKAGE_VERIFY_FAILED."
    ]
    assert "private-customer" not in errors[0]

    with pytest.raises(ClientPackageError) as exc_info:
        build_client_package(bundle, "http://server-pc:8000")
    message = str(exc_info.value)
    assert "CLIENT_PACKAGE_BUILD_FAILED" in message
    assert "private-customer" not in message

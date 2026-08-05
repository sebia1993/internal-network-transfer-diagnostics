from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest

import app as app_module
import startup_ports as ports_module
from runtime_stability import DataDirectoryLock
from startup_ports import (
    ConfigFileError,
    CURRENT_CONFIG_VERSION,
    FIREWALL_NOT_APPLICABLE,
    FIREWALL_UNKNOWN,
    PortChangeDeclined,
    PortResolution,
    StartupPortError,
    check_windows_firewall_port,
    config_requires_probe_enable_migration,
    find_available_port,
    migrate_config,
    persist_port_change,
    persist_probe_port_change,
    prompt_for_port_change,
    prompt_for_probe_port_change,
    resolve_probe_port,
    resolve_startup_port,
    rewrite_base_url_port,
)


def write_config(
    tmp_path: Path,
    *,
    base_url: str = "http://files.local:8000",
    config_version: int | None = CURRENT_CONFIG_VERSION,
    probe_enabled: bool = False,
) -> Path:
    path = tmp_path / "config.ini"
    version_line = [] if config_version is None else [f"CONFIG_VERSION={config_version}"]
    path.write_text(
        "\n".join(
            [
                "[app]",
                *version_line,
                "HOST=0.0.0.0",
                "PORT=8000",
                f"BASE_URL={base_url}",
                "STORAGE_ROOT=uploads",
                "DELETE_ALLOWED_IPS=127.0.0.1,::1",
                "RECENT_LIMIT=50",
                "CUSTOM_OPTION=preserved",
                "",
                "[network_probe]",
                f"ENABLED={'true' if probe_enabled else 'false'}",
                "PORT=5201",
                "",
                "[custom]",
                "VALUE=kept",
            ]
        ),
        encoding="utf-8",
    )
    return path


def read_config(path: Path) -> ConfigParser:
    parser = ConfigParser()
    parser.read(path, encoding="utf-8")
    return parser


def test_resolve_startup_port_keeps_available_configured_port():
    confirmations = []
    resolution = resolve_startup_port(
        "0.0.0.0",
        8000,
        availability_check=lambda host, port: port == 8000,
        existing_instance_check=lambda port: False,
        confirm_change=lambda old, new: confirmations.append((old, new)) or True,
    )

    assert resolution == PortResolution(8000, 8000)
    assert confirmations == []


def test_resolve_startup_port_detects_existing_instance_without_fallback():
    resolution = resolve_startup_port(
        "0.0.0.0",
        8000,
        availability_check=lambda host, port: False,
        existing_instance_check=lambda port: True,
        confirm_change=lambda old, new: pytest.fail("confirmation must not be requested"),
    )

    assert resolution.existing_instance
    assert not resolution.changed


def test_resolve_startup_port_selects_first_available_non_probe_port():
    checked = []

    def available(host, port):
        checked.append(port)
        return port == 8003

    resolution = resolve_startup_port(
        "0.0.0.0",
        8000,
        excluded_ports={8002},
        availability_check=available,
        existing_instance_check=lambda port: False,
        confirm_change=lambda old, new: (old, new) == (8000, 8003),
    )

    assert resolution == PortResolution(8000, 8003)
    assert checked == [8000, 8001, 8003]


def test_resolve_startup_port_decline_does_not_select_port():
    with pytest.raises(PortChangeDeclined):
        resolve_startup_port(
            "0.0.0.0",
            8000,
            availability_check=lambda host, port: port == 8001,
            existing_instance_check=lambda port: False,
            confirm_change=lambda old, new: False,
        )


def test_find_available_port_stops_after_99_candidates():
    checked = []
    result = find_available_port(
        "0.0.0.0",
        8000,
        availability_check=lambda host, port: checked.append(port) or False,
    )

    assert result is None
    assert checked == list(range(8001, 8100))


def test_resolve_startup_port_reports_exhausted_range():
    with pytest.raises(StartupPortError, match="사용할 수 있는 포트"):
        resolve_startup_port(
            "0.0.0.0",
            65535,
            availability_check=lambda host, port: False,
            existing_instance_check=lambda port: False,
        )


def test_prompt_for_port_change_accepts_enter_and_retries_invalid_input():
    answers = iter(["maybe", ""])
    messages = []

    assert prompt_for_port_change(
        8000,
        8001,
        input_func=lambda prompt: next(answers),
        output_func=messages.append,
    )
    assert messages == ["Y 또는 N을 입력하세요. Enter는 Y로 처리됩니다."]


def test_prompt_for_port_change_rejects_n_and_noninteractive_input():
    assert not prompt_for_port_change(8000, 8001, input_func=lambda prompt: "n")
    messages = []
    assert not prompt_for_port_change(8000, 8001, output_func=messages.append, interactive=False)
    assert "자동으로 변경하지 않습니다" in messages[0]


def test_resolve_probe_port_selects_available_port_and_excludes_web_port():
    checked = []

    resolution = resolve_probe_port(
        "0.0.0.0",
        5201,
        excluded_ports={5202},
        availability_check=lambda host, port: checked.append(port) or port == 5203,
        confirm_change=lambda old, new: (old, new) == (5201, 5203),
    )

    assert resolution == PortResolution(5201, 5203)
    assert checked == [5201, 5203]


def test_resolve_probe_port_treats_web_port_as_unavailable():
    resolution = resolve_probe_port(
        "0.0.0.0",
        5201,
        excluded_ports={5201},
        availability_check=lambda host, port: port in {5201, 5202},
        confirm_change=lambda old, new: True,
    )

    assert resolution == PortResolution(5201, 5202)


def test_resolve_probe_port_decline_keeps_web_server_decision_separate():
    with pytest.raises(PortChangeDeclined, match="TCP 측정 포트"):
        resolve_probe_port(
            "0.0.0.0",
            5201,
            availability_check=lambda host, port: port == 5202,
            confirm_change=lambda old, new: False,
        )


def test_prompt_for_probe_port_change_accepts_enter_and_handles_noninteractive():
    assert prompt_for_probe_port_change(5201, 5202, input_func=lambda prompt: "")
    messages = []
    assert not prompt_for_probe_port_change(
        5201,
        5202,
        output_func=messages.append,
        interactive=False,
    )
    assert "TCP 측정 포트" in messages[0]


@pytest.mark.parametrize(
    ("value", "old_port", "new_port", "expected", "has_warning"),
    [
        ("", 8000, 8001, "", False),
        ("http://files.local:8000", 8000, 8001, "http://files.local:8001", False),
        ("http://files.local:9000", 8000, 8001, "http://files.local:9000", True),
        ("http://files.local", 80, 8001, "http://files.local:8001", False),
        ("not-a-url", 8000, 8001, "not-a-url", True),
    ],
)
def test_rewrite_base_url_port(value, old_port, new_port, expected, has_warning):
    updated, warning = rewrite_base_url_port(value, old_port, new_port)

    assert updated == expected
    assert bool(warning) is has_warning


def test_persist_port_change_updates_port_base_url_and_preserves_options(tmp_path):
    path = write_config(tmp_path)

    result = persist_port_change(path, 8000, 8001)

    parser = read_config(path)
    assert parser.getint("app", "PORT") == 8001
    assert parser.get("app", "BASE_URL") == "http://files.local:8001"
    assert parser.get("app", "CUSTOM_OPTION") == "preserved"
    assert parser.getint("network_probe", "PORT") == 5201
    assert parser.get("custom", "VALUE") == "kept"
    assert result.base_url_changed
    assert not result.warning


def test_persist_port_change_creates_complete_missing_config(tmp_path):
    path = tmp_path / "config.ini"

    persist_port_change(path, 8000, 8001)

    parser = read_config(path)
    assert parser.getint("app", "PORT") == 8001
    assert parser.getint("app", "CONFIG_VERSION") == CURRENT_CONFIG_VERSION
    assert parser.get("app", "STORAGE_ROOT") == "uploads"
    assert parser.getboolean("network_probe", "ENABLED") is True
    assert parser.getint("network_probe", "PORT") == 5201


def test_legacy_config_migration_enables_probe_once(tmp_path):
    path = write_config(tmp_path, config_version=None, probe_enabled=False)

    assert config_requires_probe_enable_migration(path)
    result = migrate_config(path)

    parser = read_config(path)
    assert result.previous_version == 0
    assert result.current_version == CURRENT_CONFIG_VERSION
    assert result.probe_enabled_changed
    assert parser.getint("app", "CONFIG_VERSION") == CURRENT_CONFIG_VERSION
    assert parser.getboolean("network_probe", "ENABLED") is True
    assert parser.get("app", "CUSTOM_OPTION") == "preserved"
    assert parser.get("custom", "VALUE") == "kept"


def test_current_config_respects_user_probe_disable(tmp_path):
    path = write_config(tmp_path, probe_enabled=False)
    original = path.read_bytes()

    assert not config_requires_probe_enable_migration(path)
    result = migrate_config(path)

    assert not result.probe_enabled_changed
    assert read_config(path).getboolean("network_probe", "ENABLED") is False
    assert path.read_bytes() == original


def test_persist_probe_port_change_preserves_other_settings(tmp_path):
    path = write_config(tmp_path, probe_enabled=True)

    persist_probe_port_change(path, 5202)

    parser = read_config(path)
    assert parser.getint("network_probe", "PORT") == 5202
    assert parser.getboolean("network_probe", "ENABLED") is True
    assert parser.getint("app", "PORT") == 8000
    assert parser.get("custom", "VALUE") == "kept"


def test_persist_port_change_replace_failure_keeps_original_file(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(
        ports_module,
        "durable_replace",
        lambda source, target: (_ for _ in ()).throw(OSError("busy")),
    )

    with pytest.raises(OSError, match="busy"):
        persist_port_change(path, 8000, 8001)

    assert path.read_bytes() == original
    assert list(tmp_path.glob(".config.ini.*.tmp")) == []


def test_windows_firewall_status_does_not_spawn_a_child_process():
    assert check_windows_firewall_port(8001, platform="win32") == FIREWALL_UNKNOWN


def test_windows_firewall_status_skips_non_windows():
    assert check_windows_firewall_port(8001, platform="darwin") == FIREWALL_NOT_APPLICABLE


class FakeWebServer:
    def __init__(self, events):
        self.events = events

    def serve_forever(self):
        self.events.append("serve")

    def begin_shutdown(self):
        self.events.append("drain-start")

    def wait_for_active_requests(self, timeout_seconds):
        assert timeout_seconds == app_module.WEB_SHUTDOWN_DRAIN_SECONDS
        self.events.append("drain-wait")
        return True

    @property
    def active_request_count(self):
        return 0

    def server_close(self):
        self.events.append("close")


class StuckFakeWebServer(FakeWebServer):
    def __init__(self, events):
        super().__init__(events)
        self.wait_calls = 0
        self._active_request_count = 1

    def wait_for_active_requests(self, timeout_seconds):
        self.wait_calls += 1
        if self.wait_calls == 1:
            assert timeout_seconds == app_module.WEB_SHUTDOWN_DRAIN_SECONDS
            self.events.append("drain-wait")
            return False
        assert timeout_seconds == app_module.WEB_FORCE_CLOSE_GRACE_SECONDS
        self.events.append("force-wait")
        self._active_request_count = 0
        return True

    def force_close_active_requests(self):
        self.events.append("force-close")
        return self._active_request_count

    @property
    def active_request_count(self):
        return self._active_request_count


class PermanentlyStuckFakeWebServer(FakeWebServer):
    def wait_for_active_requests(self, timeout_seconds):
        self.events.append(("wait", timeout_seconds))
        return False

    def force_close_active_requests(self):
        self.events.append("force-close")
        return 1

    @property
    def active_request_count(self):
        return 1


def test_main_returns_failure_when_web_port_change_is_declined(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path)
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PortChangeDeclined("사용자가 웹 포트 변경을 취소했습니다.")
        ),
    )

    assert app_module.main(["--config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "WEB_PORT_CHANGE_DECLINED" in captured.err
    assert "서버를 시작하지 않았습니다" in captured.err


def test_main_binds_selected_port_before_persisting_config(tmp_path, monkeypatch):
    path = write_config(tmp_path, base_url="http://files.local:8000")
    events = []
    real_persist = ports_module.persist_port_change

    monkeypatch.setattr(app_module, "resolve_startup_port", lambda *args, **kwargs: PortResolution(8000, 8001))

    def fake_make_server(host, port, flask_app, threaded):
        assert read_config(path).getint("app", "PORT") == 8000
        assert port == 8001
        events.append("bind")
        return FakeWebServer(events)

    def persist(config_path, old_port, new_port):
        events.append("persist")
        return real_persist(config_path, old_port, new_port)

    monkeypatch.setattr(app_module, "make_server", fake_make_server)
    monkeypatch.setattr(app_module, "persist_port_change", persist)
    monkeypatch.setattr(app_module, "print_firewall_status", lambda port: events.append("firewall"))
    monkeypatch.setattr(app_module, "print_server_addresses", lambda config: events.append("addresses"))

    assert app_module.main(["--config", str(path)]) == 0
    assert read_config(path).getint("app", "PORT") == 8001
    assert events == [
        "bind",
        "persist",
        "firewall",
        "addresses",
        "serve",
        "drain-start",
        "drain-wait",
        "close",
    ]


def test_main_force_closes_requests_that_exceed_shutdown_drain(
    tmp_path,
    monkeypatch,
):
    path = write_config(tmp_path)
    events = []
    server = StuckFakeWebServer(events)

    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: server)
    monkeypatch.setattr(
        app_module,
        "print_server_addresses",
        lambda config: events.append("addresses"),
    )

    assert app_module.main(["--config", str(path)]) == 0
    assert events == [
        "addresses",
        "serve",
        "drain-start",
        "drain-wait",
        "force-close",
        "force-wait",
        "close",
    ]


def test_main_hard_exits_without_releasing_data_lock_when_handler_stays_alive(
    tmp_path,
    monkeypatch,
):
    path = write_config(tmp_path)
    events = []
    server = PermanentlyStuckFakeWebServer(events)

    class ForcedExit(RuntimeError):
        pass

    class FakeDataDirectoryLock:
        def __init__(self, _path):
            events.append("lock-created")

        def acquire(self):
            events.append("lock-acquired")

        def release(self):
            events.append("lock-released")

    def fake_hard_exit(exit_code):
        events.append(("hard-exit", exit_code))
        raise ForcedExit

    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(app_module, "DataDirectoryLock", FakeDataDirectoryLock)
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: server)
    monkeypatch.setattr(app_module, "print_server_addresses", lambda config: None)
    monkeypatch.setattr(app_module, "hard_exit_process", fake_hard_exit)

    with pytest.raises(ForcedExit):
        app_module.main(["--config", str(path)])

    assert ("hard-exit", 2) in events
    assert "lock-released" not in events
    assert "close" not in events


def test_main_reports_corrupt_upload_transaction_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path)
    config = app_module.load_config(path)
    transaction_root = config.log_path.parent / "upload_transactions"
    transaction_root.mkdir(parents=True)
    (transaction_root / "broken.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )

    assert app_module.main(["--config", str(path)]) == 2

    error_output = capsys.readouterr().err
    assert "업로드 복구 기록이 손상" in error_output
    assert "Traceback" not in error_output
    assert "{not-json" not in error_output
    assert "broken.json" not in error_output
    diagnostic_log = (
        config.log_path.parent / "diagnostics" / "internal-upload.log"
    ).read_text(encoding="utf-8")
    assert "startup_upload_transaction_recovery_failed" in diagnostic_log
    assert "{not-json" not in diagnostic_log


def test_main_reports_corrupt_measurement_transaction_without_traceback(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path)
    config = app_module.load_config(path)
    app_module.ensure_directories(config)
    transaction_root = (
        config.log_path.parent
        / "measurement_transactions"
        / "http_sustained"
    )
    transaction_root.mkdir(parents=True)
    (transaction_root / "broken.json").write_text(
        "{private-not-json",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )

    assert app_module.main(["--config", str(path)]) == 2

    error_output = capsys.readouterr().err
    assert "MEASUREMENT_RECOVERY_FAILED" in error_output
    assert "Traceback" not in error_output
    assert "private-not-json" not in error_output
    assert "broken.json" not in error_output
    diagnostic_log = (
        config.log_path.parent / "diagnostics" / "internal-upload.log"
    ).read_text(encoding="utf-8")
    assert "startup_measurement_transaction_recovery_failed" in diagnostic_log
    assert "private-not-json" not in diagnostic_log


def test_main_bind_failure_does_not_change_config(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(app_module, "resolve_startup_port", lambda *args, **kwargs: PortResolution(8000, 8001))
    monkeypatch.setattr(
        app_module,
        "make_server",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError(r"C:\private-customer\bind-detail")
        ),
    )

    assert app_module.main(["--config", str(path)]) == 2
    assert path.read_bytes() == original
    error_output = capsys.readouterr().err
    assert "WEB_BIND_FAILED" in error_output
    assert "private-customer" not in error_output
    assert "Traceback" not in error_output

    diagnostic_log = (
        tmp_path / "data" / "diagnostics" / "internal-upload.log"
    ).read_text(encoding="utf-8")
    assert "web_server_bind_failed error_type=OSError" in diagnostic_log
    assert "private-customer" not in diagnostic_log


def test_main_reports_diagnostic_log_init_failure_without_exception_or_path(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path)
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(
        app_module,
        "configure_diagnostic_logger",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PermissionError(r"C:\private-customer\diagnostics")
        ),
    )

    assert app_module.main(["--config", str(path)]) == 2

    error_output = capsys.readouterr().err
    assert "DIAGNOSTIC_LOG_INIT_FAILED" in error_output
    assert "private-customer" not in error_output
    assert "Traceback" not in error_output

    lock = DataDirectoryLock(
        tmp_path / "data" / ".internal-upload.instance.lock"
    )
    lock.acquire()
    lock.release()


def test_main_web_port_persist_failure_is_sanitized_and_closes_server(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path)
    original = path.read_bytes()
    events = []
    server = FakeWebServer(events)
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8001),
    )
    monkeypatch.setattr(
        app_module,
        "make_server",
        lambda *args, **kwargs: server,
    )
    monkeypatch.setattr(
        app_module,
        "persist_port_change",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PermissionError(r"C:\private-customer\config.ini")
        ),
    )

    assert app_module.main(["--config", str(path)]) == 2

    assert path.read_bytes() == original
    assert "close" in events
    error_output = capsys.readouterr().err
    assert "WEB_PORT_CONFIG_WRITE_FAILED" in error_output
    assert "private-customer" not in error_output
    assert "Traceback" not in error_output
    diagnostic_log = (
        tmp_path / "data" / "diagnostics" / "internal-upload.log"
    ).read_text(encoding="utf-8")
    assert "web_port_config_write_failed error_type=PermissionError" in diagnostic_log
    assert "private-customer" not in diagnostic_log


def test_main_reports_config_path_resolution_failure_without_traceback(
    monkeypatch,
    capsys,
):
    class FailingPath:
        def __init__(self, *_args, **_kwargs):
            pass

        def resolve(self):
            raise OSError(r"C:\private-customer\config.ini")

    monkeypatch.setattr(app_module, "Path", FailingPath)

    assert app_module.main(["--config", "config.ini"]) == 2

    error_output = capsys.readouterr().err
    assert "CONFIG_PATH_INVALID" in error_output
    assert "private-customer" not in error_output
    assert "Traceback" not in error_output


def test_main_existing_instance_exits_without_binding(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000, existing_instance=True),
    )
    monkeypatch.setattr(app_module, "print_server_addresses", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: pytest.fail("must not bind"))

    assert app_module.main(["--config", str(path)]) == 0


@pytest.mark.parametrize("health_status", ["ok", "degraded"])
def test_existing_instance_detection_accepts_healthy_and_degraded_app(
    monkeypatch,
    health_status,
):
    class FakeResponse:
        status = 200

        def read(self, _limit):
            return (
                '{"app":"internal-upload","status":"%s","port":8000}'
                % health_status
            ).encode("utf-8")

    class FakeConnection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(ports_module.http.client, "HTTPConnection", FakeConnection)

    assert ports_module.is_existing_instance(8000) is True


def test_main_rejects_second_instance_using_same_data_directory(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    existing_lock = DataDirectoryLock(
        tmp_path / "data" / ".internal-upload.instance.lock"
    )
    existing_lock.acquire()
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: pytest.fail("must not bind"))

    try:
        assert app_module.main(["--config", str(path)]) == 2
    finally:
        existing_lock.release()


def test_main_starts_probe_on_approved_fallback_then_persists_port(tmp_path, monkeypatch):
    path = write_config(tmp_path, probe_enabled=True)
    events = []

    class FakeProbeService:
        def __init__(self, *, config, measurement_gate, normalize_ip):
            self.config = config
            self.start_error = ""

        def start(self):
            events.append(("probe-start", self.config.port))
            return True

        def stop(self):
            events.append("probe-stop")

    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(
        app_module,
        "resolve_probe_port",
        lambda *args, **kwargs: PortResolution(5201, 5202),
    )
    monkeypatch.setattr(app_module, "ProbeService", FakeProbeService)
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: FakeWebServer(events))
    monkeypatch.setattr(app_module, "print_firewall_status", lambda port: events.append(("firewall", port)))
    monkeypatch.setattr(app_module, "print_server_addresses", lambda config: events.append("addresses"))

    assert app_module.main(["--config", str(path)]) == 0

    assert read_config(path).getint("network_probe", "PORT") == 5202
    assert events == [
        ("probe-start", 5202),
        ("firewall", 5202),
        "addresses",
        "serve",
        "drain-start",
        "drain-wait",
        "probe-stop",
        "close",
    ]


def test_main_probe_port_persist_failure_is_sanitized_and_continues(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path, probe_enabled=True)
    events = []

    class FakeProbeService:
        def __init__(self, *, config, measurement_gate, normalize_ip):
            self.config = config
            self.start_error = ""

        def start(self):
            events.append(("probe-start", self.config.port))
            return True

        def stop(self):
            events.append("probe-stop")

    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(
        app_module,
        "resolve_probe_port",
        lambda *args, **kwargs: PortResolution(5201, 5202),
    )
    monkeypatch.setattr(app_module, "ProbeService", FakeProbeService)
    monkeypatch.setattr(
        app_module,
        "make_server",
        lambda *args, **kwargs: FakeWebServer(events),
    )
    monkeypatch.setattr(app_module, "print_firewall_status", lambda *args: None)
    monkeypatch.setattr(app_module, "print_server_addresses", lambda *args: None)
    monkeypatch.setattr(
        app_module,
        "persist_probe_port_change",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PermissionError(r"C:\private-customer\config.ini")
        ),
    )

    assert app_module.main(["--config", str(path)]) == 0

    assert read_config(path).getint("network_probe", "PORT") == 5201
    assert "serve" in events
    error_output = capsys.readouterr().err
    assert "PROBE_PORT_CONFIG_WRITE_FAILED" in error_output
    assert "private-customer" not in error_output
    assert "Traceback" not in error_output
    diagnostic_log = (
        tmp_path / "data" / "diagnostics" / "internal-upload.log"
    ).read_text(encoding="utf-8")
    assert "probe_port_config_write_failed error_type=PermissionError" in diagnostic_log
    assert "private-customer" not in diagnostic_log


def test_main_keeps_web_server_running_when_probe_port_change_is_declined(tmp_path, monkeypatch):
    path = write_config(tmp_path, probe_enabled=True)
    events = []

    class FakeProbeService:
        def __init__(self, *, config, measurement_gate, normalize_ip):
            self.config = config
            self.start_error = ""

        def start(self):
            pytest.fail("probe must not start after port change is declined")

        def stop(self):
            events.append("probe-stop")

    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(
        app_module,
        "resolve_probe_port",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PortChangeDeclined("사용자가 TCP 측정 포트 변경을 취소했습니다.")
        ),
    )
    monkeypatch.setattr(app_module, "ProbeService", FakeProbeService)
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: FakeWebServer(events))
    monkeypatch.setattr(app_module, "print_server_addresses", lambda config: events.append("addresses"))

    assert app_module.main(["--config", str(path)]) == 0

    assert read_config(path).getint("network_probe", "PORT") == 5201
    assert events == [
        "addresses",
        "serve",
        "drain-start",
        "drain-wait",
        "probe-stop",
        "close",
    ]


def test_main_keeps_migrated_probe_enabled_when_config_write_fails(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path, config_version=None, probe_enabled=False)
    events = []

    class FakeProbeService:
        def __init__(self, *, config, measurement_gate, normalize_ip):
            assert config.enabled is True
            self.config = config
            self.start_error = ""

        def start(self):
            events.append("probe-start")
            return True

        def stop(self):
            events.append("probe-stop")

    monkeypatch.setattr(app_module, "migrate_config", lambda path: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(
        app_module,
        "resolve_probe_port",
        lambda *args, **kwargs: PortResolution(5201, 5201),
    )
    monkeypatch.setattr(app_module, "ProbeService", FakeProbeService)
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: FakeWebServer(events))
    monkeypatch.setattr(app_module, "print_firewall_status", lambda port: None)
    monkeypatch.setattr(app_module, "print_server_addresses", lambda config: None)

    assert app_module.main(["--config", str(path)]) == 0

    parser = read_config(path)
    assert not parser.has_option("app", "CONFIG_VERSION")
    assert parser.getboolean("network_probe", "ENABLED") is False
    assert events == [
        "probe-start",
        "serve",
        "drain-start",
        "drain-wait",
        "probe-stop",
        "close",
    ]
    error_output = capsys.readouterr().err
    assert "[app] CONFIG_VERSION" in error_output
    assert "[network_probe] ENABLED" in error_output
    assert path.name in error_output
    assert str(path.resolve()) not in error_output
    assert "read-only" not in error_output
    assert "Traceback" not in error_output


def test_main_does_not_persist_fallback_probe_port_when_bind_fails(tmp_path, monkeypatch):
    path = write_config(tmp_path, probe_enabled=True)
    events = []

    class FakeProbeService:
        def __init__(self, *, config, measurement_gate, normalize_ip):
            self.config = config
            self.start_error = "bind failed"

        def start(self):
            events.append("probe-start-failed")
            return False

        def stop(self):
            events.append("probe-stop")

    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )
    monkeypatch.setattr(
        app_module,
        "resolve_probe_port",
        lambda *args, **kwargs: PortResolution(5201, 5202),
    )
    monkeypatch.setattr(app_module, "ProbeService", FakeProbeService)
    monkeypatch.setattr(app_module, "make_server", lambda *args, **kwargs: FakeWebServer(events))
    monkeypatch.setattr(app_module, "print_server_addresses", lambda config: None)

    assert app_module.main(["--config", str(path)]) == 0

    assert read_config(path).getint("network_probe", "PORT") == 5201
    assert events == [
        "probe-start-failed",
        "serve",
        "drain-start",
        "drain-wait",
        "probe-stop",
        "close",
    ]


@pytest.mark.parametrize(
    ("old_value", "new_value", "expected_key", "expected_allowed"),
    [
        ("PORT=8000", "PORT=not-a-number", "[app] PORT", "1~65535 사이의 정수"),
        ("PORT=8000", "PORT=0", "[app] PORT", "1~65535 사이의 정수"),
        ("RECENT_LIMIT=50", "RECENT_LIMIT=0", "[app] RECENT_LIMIT", "1~10000 사이의 정수"),
        ("RECENT_LIMIT=50", "RECENT_LIMIT=10001", "[app] RECENT_LIMIT", "1~10000 사이의 정수"),
        ("CONFIG_VERSION=2", "CONFIG_VERSION=3", "[app] CONFIG_VERSION", "0~2 사이의 정수"),
        ("HOST=0.0.0.0", "HOST=http://host", "[app] HOST", "포트가 없는 호스트 이름"),
        ("BASE_URL=http://files.local:8000", "BASE_URL=file:///tmp", "[app] BASE_URL", "http(s) URL"),
        ("STORAGE_ROOT=uploads", "STORAGE_ROOT=   ", "[app] STORAGE_ROOT", "비어 있지 않은 폴더 경로"),
        (
            "DELETE_ALLOWED_IPS=127.0.0.1,::1",
            "DELETE_ALLOWED_IPS=127.0.0.0/8",
            "[app] DELETE_ALLOWED_IPS",
            "CIDR 제외",
        ),
        (
            "ENABLED=false",
            "ENABLED=maybe",
            "[network_probe] ENABLED",
            "true/false, yes/no, on/off 또는 1/0",
        ),
        ("PORT=5201", "PORT=70000", "[network_probe] PORT", "1~65535 사이의 정수"),
    ],
)
def test_main_rejects_invalid_explicit_config_values_without_fallback(
    tmp_path,
    monkeypatch,
    capsys,
    old_value,
    new_value,
    expected_key,
    expected_allowed,
):
    path = write_config(tmp_path)
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(old_value, new_value, 1) + "\nPRIVATE_VALUE=do-not-print\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: pytest.fail("must not resolve a port"),
    )

    assert app_module.main(["--config", str(path)]) == 2

    error_output = capsys.readouterr().err
    assert expected_key in error_output
    assert expected_allowed in error_output
    assert path.name in error_output
    assert str(path.resolve()) not in error_output
    assert "do-not-print" not in error_output
    assert "Traceback" not in error_output


def test_load_config_rejects_invalid_explicit_value(tmp_path):
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("RECENT_LIMIT=50", "RECENT_LIMIT=invalid"),
        encoding="utf-8",
    )

    with pytest.raises(ConfigFileError, match=r"\[app\] RECENT_LIMIT"):
        app_module.load_config(path)


@pytest.mark.parametrize(
    "base_url",
    [
        "javascript:alert(1)",
        "http://user:password@files.local:8000",
        "http://files.local:8000?token=private",
        "http://files.local:8000#fragment",
        "http://files.local:not-a-port",
    ],
)
def test_load_config_rejects_unsafe_base_url_without_echoing_value(
    tmp_path,
    base_url,
):
    path = write_config(tmp_path, base_url=base_url)

    with pytest.raises(ConfigFileError) as exc_info:
        app_module.load_config(path)

    message = str(exc_info.value)
    assert "[app] BASE_URL" in message
    assert base_url not in message


def test_load_config_rejects_storage_root_that_is_a_file(tmp_path):
    storage_file = tmp_path / "not-a-folder"
    storage_file.write_text("private-content", encoding="utf-8")
    path = write_config(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "STORAGE_ROOT=uploads",
            "STORAGE_ROOT=not-a-folder",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigFileError) as exc_info:
        app_module.load_config(path)

    message = str(exc_info.value)
    assert "[app] STORAGE_ROOT" in message
    assert "private-content" not in message


def test_main_reports_storage_initialization_failure_without_traceback_or_path(
    tmp_path,
    monkeypatch,
    capsys,
):
    path = write_config(tmp_path)
    monkeypatch.setattr(
        app_module,
        "resolve_startup_port",
        lambda *args, **kwargs: PortResolution(8000, 8000),
    )

    def fail_storage_initialization(_config):
        raise PermissionError(r"C:\private-customer\denied")

    monkeypatch.setattr(
        app_module,
        "ensure_directories",
        fail_storage_initialization,
    )

    assert app_module.main(["--config", str(path)]) == 2

    error_output = capsys.readouterr().err
    assert "STORAGE_INIT_FAILED" in error_output
    assert "private-customer" not in error_output
    assert "Traceback" not in error_output


@pytest.mark.parametrize(
    ("content", "expected_text"),
    [
        (
            "[app]\nPORT=8000\n[app]\nPRIVATE_VALUE=do-not-print\n",
            "설정 파일 형식이 올바르지 않습니다",
        ),
        (
            "PORT=8000\nPRIVATE_VALUE=do-not-print\n",
            "설정 파일 형식이 올바르지 않습니다",
        ),
    ],
)
def test_main_rejects_malformed_ini_without_exposing_content(
    tmp_path,
    capsys,
    content,
    expected_text,
):
    path = tmp_path / "config.ini"
    path.write_text(content, encoding="utf-8")

    assert app_module.main(["--config", str(path)]) == 2

    error_output = capsys.readouterr().err
    assert expected_text in error_output
    assert "[app]" in error_output
    assert "[network_probe]" in error_output
    assert path.name in error_output
    assert str(path.resolve()) not in error_output
    assert "do-not-print" not in error_output
    assert "Traceback" not in error_output


def test_main_rejects_non_utf8_config_without_traceback(tmp_path, capsys):
    path = tmp_path / "config.ini"
    path.write_bytes(b"[app]\nPORT=8000\nPRIVATE_VALUE=\xff\xfe\n")

    assert app_module.main(["--config", str(path)]) == 2

    error_output = capsys.readouterr().err
    assert "허용값: UTF-8" in error_output
    assert path.name in error_output
    assert str(path.resolve()) not in error_output
    assert "Traceback" not in error_output


def test_main_reports_config_read_oserror_without_traceback(tmp_path, capsys):
    config_directory = tmp_path / "config.ini"
    config_directory.mkdir()

    assert app_module.main(["--config", str(config_directory)]) == 2

    error_output = capsys.readouterr().err
    assert "설정 파일을 읽을 수 없습니다" in error_output
    assert "읽기 가능한 UTF-8 INI 파일" in error_output
    assert config_directory.name in error_output
    assert str(config_directory.resolve()) not in error_output
    assert "Traceback" not in error_output

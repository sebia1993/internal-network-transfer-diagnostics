from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROBE_PROTOCOL_VERSION = 3
PROBE_DIRECTIONS = ("upload", "download", "full")
PROBE_DURATIONS = (10, 30)
PROBE_STREAM_COUNTS = (1, 4)
PROBE_WARMUP_SECONDS = 3.0
PROBE_CONNECTIVITY_INTERVAL_SECONDS = 20.0
PROBE_CONNECTIVITY_STALE_SECONDS = 45.0
PROBE_CONNECTIVITY_TIMEOUT_SECONDS = 3.0
PROBE_TERMINAL_SESSION_TTL_SECONDS = 30 * 60.0
PROBE_MAX_TERMINAL_SESSIONS = 100
PROBE_MAX_CONNECTION_HANDLERS = 16


@dataclass(frozen=True)
class ProbeConfig:
    enabled: bool
    host: str
    port: int
    log_path: Path
    results_root: Path
    warmup_seconds: float = PROBE_WARMUP_SECONDS
    agent_ttl_seconds: float = 45.0
    long_poll_seconds: float = 20.0
    stream_attach_timeout_seconds: float = 10.0
    terminal_session_ttl_seconds: float = PROBE_TERMINAL_SESSION_TTL_SECONDS
    max_terminal_sessions: int = PROBE_MAX_TERMINAL_SESSIONS
    max_connection_handlers: int = PROBE_MAX_CONNECTION_HANDLERS


@dataclass
class AgentRecord:
    agent_id: str
    token: str
    tcp_hmac_key: str
    hostname: str
    client_ip: str
    server_host: str
    protocol_version: int
    client_version: str
    registered_at: float
    last_seen_at: float
    connectivity_status: str = "checking"
    connectivity_checked_at: float | None = None
    connectivity_error_code: str = ""
    connectivity_message: str = ""
    busy_session_id: str = ""
    pending_job: dict[str, Any] | None = None


@dataclass
class ProbeSession:
    session_id: str
    session_token: str
    agent_id: str
    agent_hostname: str
    client_ip: str
    server_host: str
    requested_direction: str
    duration_seconds: int
    stream_count: int
    created_at_monotonic: float
    created_at_text: str
    status: str = "queued"
    active_phase: str = ""
    phase_started_at: float = 0.0
    error: str = ""
    job_claimed: bool = False
    completed_at_monotonic: float | None = None
    result_available: bool = False
    persistence_complete: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)
    sockets: dict[str, dict[int, Any]] = field(default_factory=dict)
    server_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    agent_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    combined_results: dict[str, dict[str, Any]] = field(default_factory=dict)

    def phases(self) -> list[str]:
        if self.requested_direction == "full":
            return ["upload", "download"]
        return [self.requested_direction]

    def next_phase(self) -> str | None:
        for phase in self.phases():
            if phase not in self.combined_results:
                return phase
        return None

import socket
import threading
import time

from bounded_server import make_bounded_server


def simple_app(_environ, start_response):
    payload = b"ok"
    start_response(
        "200 OK",
        [("Content-Type", "text/plain"), ("Content-Length", str(len(payload)))],
    )
    return [payload]


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def receive_all(connection):
    chunks = []
    while True:
        try:
            chunk = connection.recv(4096)
        except (ConnectionAbortedError, ConnectionResetError, TimeoutError):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def test_bounded_server_rejects_excess_slow_clients_and_recovers_capacity():
    server = make_bounded_server(
        "127.0.0.1",
        0,
        simple_app,
        max_request_threads=2,
        request_timeout_seconds=0.4,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    address = server.server_address
    slow_connections = []
    try:
        for _ in range(2):
            connection = socket.create_connection(address, timeout=2)
            connection.settimeout(2)
            connection.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n")
            slow_connections.append(connection)

        assert wait_until(lambda: server.active_request_count == 2)

        for _ in range(10):
            rejected = socket.create_connection(address, timeout=2)
            rejected.settimeout(2)
            rejected.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            rejected_payload = receive_all(rejected)
            rejected.close()
            assert b"503 Service Unavailable" in rejected_payload
        assert server.rejected_request_count == 10
        assert server.active_request_count <= 2
        assert wait_until(lambda: server.active_request_count == 0, timeout=2)

        recovered = socket.create_connection(address, timeout=2)
        recovered.settimeout(2)
        recovered.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        recovered_payload = receive_all(recovered)
        recovered.close()

        assert b"200 OK" in recovered_payload
        assert recovered_payload.endswith(b"ok")
    finally:
        for connection in slow_connections:
            connection.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)

    assert not server_thread.is_alive()


def test_bounded_server_rejects_new_requests_and_drains_active_request():
    request_started = threading.Event()
    release_request = threading.Event()

    def blocking_app(_environ, start_response):
        request_started.set()
        release_request.wait(timeout=3)
        return simple_app(_environ, start_response)

    server = make_bounded_server(
        "127.0.0.1",
        0,
        blocking_app,
        max_request_threads=2,
        request_timeout_seconds=2,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    active = socket.create_connection(server.server_address, timeout=2)
    active.settimeout(3)
    try:
        active.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        assert request_started.wait(timeout=2)
        assert server.active_request_count == 1

        server.begin_shutdown()
        assert server.is_draining is True

        for _ in range(10):
            rejected = socket.create_connection(server.server_address, timeout=2)
            rejected.settimeout(2)
            rejected.sendall(
                b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
            )
            rejected_payload = receive_all(rejected)
            rejected.close()
            assert b"503 Service Unavailable" in rejected_payload
            assert b"Server is shutting down" in rejected_payload
        assert server.wait_for_active_requests(timeout_seconds=0.05) is False

        release_request.set()
        assert receive_all(active).endswith(b"ok")
        assert server.wait_for_active_requests(timeout_seconds=2) is True
        assert server.active_request_count == 0
    finally:
        release_request.set()
        active.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)

    assert not server_thread.is_alive()


def test_bounded_server_force_closes_slow_request_after_drain_timeout():
    server = make_bounded_server(
        "127.0.0.1",
        0,
        simple_app,
        max_request_threads=1,
        request_timeout_seconds=30,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    slow = socket.create_connection(server.server_address, timeout=2)
    slow.settimeout(2)
    try:
        slow.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1024\r\n")
        assert wait_until(lambda: server.active_request_count == 1)
        # Production force-close runs only after the 30-second drain window.
        # Let the request handler enter its blocking header read before forcing.
        time.sleep(0.05)

        server.begin_shutdown()
        assert server.wait_for_active_requests(timeout_seconds=0.05) is False
        assert server.force_close_active_requests() == 1
        # Windows buffered header reads can outlive socket.shutdown(). The
        # application-level shutdown path must hard-exit without releasing the
        # data lock if the client never disconnects.
        if not server.wait_for_active_requests(timeout_seconds=0.1):
            slow.close()
        assert server.wait_for_active_requests(timeout_seconds=2) is True
        assert server.active_request_count == 0
    finally:
        slow.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)

    assert not server_thread.is_alive()


def test_force_shutdown_keeps_socket_object_valid_for_handler_cleanup(capsys):
    request_started = threading.Event()
    release_request = threading.Event()

    def blocking_app(_environ, start_response):
        request_started.set()
        release_request.wait(timeout=3)
        return simple_app(_environ, start_response)

    server = make_bounded_server(
        "127.0.0.1",
        0,
        blocking_app,
        max_request_threads=1,
        request_timeout_seconds=2,
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    client = socket.create_connection(server.server_address, timeout=2)
    client.settimeout(2)
    try:
        client.sendall(
            b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        assert request_started.wait(timeout=2)
        assert server.force_close_active_requests() == 1
        release_request.set()
        assert server.wait_for_active_requests(timeout_seconds=2) is True
    finally:
        release_request.set()
        client.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)

    error_output = capsys.readouterr().err
    assert "Traceback" not in error_output
    assert "Invalid file descriptor" not in error_output
    assert not server_thread.is_alive()

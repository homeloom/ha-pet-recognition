"""Tests of the container's health check."""

from __future__ import annotations

import runpy
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pet_engine import healthcheck
from pet_engine.healthcheck import PORT, healthy, main


class _Engine(BaseHTTPRequestHandler):
    """Answers /health with the status of its server, other paths with 404."""

    def do_GET(self) -> None:
        status = self.server.status if self.path == "/health" else 404  # type: ignore[attr-defined]
        self.send_response(status)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        """Keep the test output quiet."""


@pytest.fixture
def engine() -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    server.status = 200  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def port_of(server: ThreadingHTTPServer) -> str:
    return str(server.server_address[1])


def test_an_engine_that_answers_is_healthy(
    engine: ThreadingHTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert healthy({"ENGINE_PORT": port_of(engine)})
    monkeypatch.setenv("ENGINE_PORT", port_of(engine))
    assert main() == 0


@pytest.mark.parametrize("status", [204, 503])
def test_an_engine_that_answers_otherwise_is_not(engine: ThreadingHTTPServer, status: int) -> None:
    engine.status = status  # type: ignore[attr-defined]
    assert not healthy({"ENGINE_PORT": port_of(engine)})


def free_port() -> str:
    """A port where nothing listens."""
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        return str(free.getsockname()[1])


def test_no_engine_is_not_healthy() -> None:
    assert not healthy({"ENGINE_PORT": free_port()})
    assert not healthy({"ENGINE_PORT": "not a port"})


# runpy warns that the module was imported before, by the tests themselves.
@pytest.mark.filterwarnings("ignore:.*found in sys.modules:RuntimeWarning")
def test_the_module_exits_with_1_when_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGINE_PORT", free_port())
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("pet_engine.healthcheck", run_name="__main__")
    assert exited.value.code == 1


def test_the_default_port_is_the_engine_s(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def refuse(url: str, timeout: float) -> None:
        asked.append(url)
        raise ConnectionRefusedError

    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", refuse)
    assert not healthy({})
    assert asked == [f"http://127.0.0.1:{PORT}/health"]

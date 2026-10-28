"""Tests of starting the engine: key, privileges, announcement and wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import runpy
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from helpers import dinov2, rows_for, yolox

from pet_engine import app as engine_app
from pet_engine.app import announce, api_key, build, drop_privileges, main
from pet_engine.detector import Detector
from pet_engine.embedder import Embedder
from pet_engine.sessions import load

KEY = "k" * 32

Client = Callable[..., Awaitable[TestClient]]


def test_the_environment_key_comes_first(tmp_path: Path) -> None:
    (tmp_path / "options.json").write_text(json.dumps({"api_key": "o" * 20}))
    assert api_key(tmp_path, {"API_KEY": f" {KEY} "}) == KEY
    assert not (tmp_path / "api_key").exists()


def test_the_option_key_comes_next(tmp_path: Path) -> None:
    (tmp_path / "options.json").write_text(json.dumps({"api_key": " " + "o" * 20}))
    (tmp_path / "api_key").write_text("s" * 20)
    assert api_key(tmp_path, {}) == "o" * 20


@pytest.mark.parametrize("options", ['{"api_key": ""}', '{"api_key": null}', "[]", None])
def test_a_key_is_created_once_and_kept(tmp_path: Path, options: str | None) -> None:
    if options is not None:
        (tmp_path / "options.json").write_text(options)
    created = api_key(tmp_path, {"API_KEY": ""})
    stored = tmp_path / "api_key"
    assert len(created) >= 32
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert stored.read_text() == created
    assert api_key(tmp_path, {}) == created


@pytest.mark.parametrize("content", ["", " \n"])
def test_an_empty_stored_key_is_replaced(tmp_path: Path, content: str) -> None:
    # Left by a start cut short before the key was written.
    stored = tmp_path / "api_key"
    stored.write_text(content)
    created = api_key(tmp_path, {})
    assert len(created) >= 32
    assert stored.read_text() == created
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == ["api_key"]


def test_a_short_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least 16"):
        api_key(tmp_path, {"API_KEY": "short"})


def test_privileges_are_dropped_only_by_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        engine_app.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=101, pw_gid=102)
    )
    for name in ("chown", "setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda *args, name=name: calls.append((name, args)))
    (tmp_path / "api_key").write_text(KEY)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    drop_privileges("engine", tmp_path / "api_key")
    assert calls == []

    (tmp_path / "api_key").unlink()
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    drop_privileges("engine", tmp_path / "api_key")
    assert calls == [("setgroups", ([],)), ("setgid", (102,)), ("setuid", (101,))]

    calls.clear()
    (tmp_path / "api_key").write_text(KEY)
    drop_privileges("engine", tmp_path / "api_key")
    assert calls[0] == ("chown", (tmp_path / "api_key", 101, 102))


class FakeSupervisor:
    """The two Supervisor routes the engine calls."""

    def __init__(self, failures: int = 0, *, refused: bool = False, slow: bool = False) -> None:
        self.failures = failures
        self.refused = refused
        self.slow = slow
        self.announced: list[dict[str, Any]] = []
        self.tokens: list[str] = []
        self.app = web.Application()
        self.app.router.add_get("/addons/self/info", self.info)
        self.app.router.add_post("/discovery", self.discovery)

    async def info(self, request: web.Request) -> web.Response:
        self.tokens.append(request.headers["Authorization"])
        if self.slow:
            await asyncio.sleep(5)
        if self.failures:
            self.failures -= 1
            return web.json_response({"result": "error"}, status=500)
        return web.json_response({"result": "ok", "data": {"hostname": "abc-pet-engine"}})

    async def discovery(self, request: web.Request) -> web.Response:
        self.announced.append(await request.json())
        if self.refused:
            return web.json_response({"result": "error"}, status=400)
        return web.json_response({"result": "ok", "data": {"uuid": "id-1"}})


async def serve(supervisor: FakeSupervisor) -> TestServer:
    server = TestServer(supervisor.app)
    await server.start_server()
    return server


async def test_the_engine_announces_its_address_and_key() -> None:
    supervisor = FakeSupervisor(failures=1)
    server = await serve(supervisor)
    try:
        async with ClientSession() as session:
            base = str(server.make_url("")).rstrip("/")
            assert await announce(session, "token", 8765, KEY, base=base, delay=0) == "id-1"
    finally:
        await server.close()
    assert supervisor.tokens == ["Bearer token"] * 2
    assert supervisor.announced == [
        {
            "service": "pet_recognition",
            "config": {"host": "abc-pet-engine", "port": 8765, "api_key": KEY},
        }
    ]


async def test_the_engine_gives_up_announcing_and_says_how_to_go_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    supervisor = FakeSupervisor(failures=5)
    server = await serve(supervisor)
    try:
        async with ClientSession() as session:
            base = str(server.make_url("")).rstrip("/")
            assert await announce(session, "token", 1, KEY, base=base, attempts=2, delay=0) is None
    finally:
        await server.close()
    assert "To add the integration by hand, set the app's API key option" in caplog.text
    assert "http://<this app's hostname>:1" in caplog.text
    assert KEY not in caplog.text


async def test_a_refused_announcement_logs_the_address_to_enter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    supervisor = FakeSupervisor(refused=True)
    server = await serve(supervisor)
    try:
        async with ClientSession() as session:
            base = str(server.make_url("")).rstrip("/")
            assert await announce(session, "token", 8765, KEY, base=base, attempts=1) is None
    finally:
        await server.close()
    assert "http://abc-pet-engine:8765" in caplog.text
    assert KEY not in caplog.text


async def test_a_supervisor_that_does_not_answer_is_given_up_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = await serve(FakeSupervisor(slow=True))
    try:
        async with ClientSession(timeout=ClientTimeout(total=0.05)) as session:
            base = str(server.make_url("")).rstrip("/")
            assert await announce(session, "token", 1, KEY, base=base, attempts=2, delay=0) is None
    finally:
        await server.close()
    assert "failed (2/2)" in caplog.text


def environment(tmp_path: Path, **extra: str) -> dict[str, str]:
    """An environment with generated models, as the app's image has real ones."""
    yolox(tmp_path / "yolox.onnx", rows_for())
    dinov2(tmp_path / "dinov2.onnx")
    data = tmp_path / "data"
    data.mkdir()
    return {
        "ENGINE_DATA": str(data),
        "ENGINE_MODELS": str(tmp_path),
        "ENGINE_VERSION": "1.2.3",
        "ENGINE_PORT": "9000",
        **extra,
    }


@pytest.fixture
def engine_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(_manifest: Path, directory: Path) -> tuple[Detector, Embedder]:
        return (
            Detector(load(directory / "yolox.onnx")),
            Embedder(load(directory / "dinov2.onnx"), "test-model"),
        )

    monkeypatch.setattr(engine_app, "load_engine", fake)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)


@pytest.mark.usefixtures("engine_models")
async def test_the_engine_is_built_from_its_environment(
    tmp_path: Path, aiohttp_client: Client
) -> None:
    application, port = build(environment(tmp_path, API_KEY=KEY))
    assert port == 9000
    client = await aiohttp_client(application)
    info = await (await client.get("/v1/info", headers={"Authorization": f"Bearer {KEY}"})).json()
    assert (info["version"], info["model"]) == ("1.2.3", "test-model")


@pytest.mark.usefixtures("engine_models")
async def test_under_the_supervisor_the_engine_announces_itself(
    tmp_path: Path, aiohttp_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, str]] = []
    done = asyncio.Event()

    async def fake(_session: ClientSession, token: str, port: int, key: str) -> str:
        calls.append((token, port, key))
        done.set()
        await asyncio.sleep(3600)
        return "never"

    monkeypatch.setattr(engine_app, "announce", fake)
    application, _ = build(environment(tmp_path, API_KEY=KEY, SUPERVISOR_TOKEN="token"))  # noqa: S106
    client = await aiohttp_client(application)
    await asyncio.wait_for(done.wait(), 5)
    assert calls == [("token", 9000, KEY)]
    # Stopping the engine cancels an announcement still waiting.
    await client.close()


def test_main_serves_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    served: list[dict[str, Any]] = []
    monkeypatch.setattr(engine_app, "build", lambda environ: ("app", 9000))
    monkeypatch.setattr(
        engine_app.web, "run_app", lambda app, **options: served.append({"app": app, **options})
    )
    with caplog.at_level(logging.INFO):
        main()
    assert served == [
        {
            "app": "app",
            "host": "0.0.0.0",  # noqa: S104
            "port": 9000,
            "access_log": None,
            "print": None,
            "handler_cancellation": False,
        }
    ]


def test_the_package_runs_as_a_module(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[bool] = []
    monkeypatch.setattr(engine_app, "main", lambda: started.append(True))
    runpy.run_module("pet_engine", run_name="__main__")
    assert started == [True]

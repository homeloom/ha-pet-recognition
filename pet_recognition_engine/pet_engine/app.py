"""Start the engine: its key, its models, its server, and its announcement.

Under the Supervisor the app announces itself to Home Assistant through the
discovery of the Supervisor (host, port and key), so nothing has to be typed.
Elsewhere, such as Home Assistant in a container, the same image runs with
Docker and the user enters the URL and the key in the integration.

The key comes from the API_KEY variable, else from the app's api_key option,
else from /data/api_key, created on the first start. The app starts as root
only to prepare that file, then runs as an unprivileged user.

Settings, from the environment: ENGINE_MODELS (/models), ENGINE_MANIFEST
(/app/models.json), ENGINE_DATA (/data), ENGINE_PORT (8765), ENGINE_USER
(engine) and ENGINE_VERSION.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import pwd
import secrets
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import aiohttp
from aiohttp import web

from .selftest import load_engine
from .server import MIN_KEY_LENGTH, Engine, create_app

SERVICE = "pet_recognition"
SUPERVISOR = "http://supervisor"
KEY_FILE = "api_key"
PORT = 8765
# Seconds for each call to the Supervisor.
ANNOUNCE_TIMEOUT = 10

_LOGGER = logging.getLogger(__name__)


def api_key(data: Path, environ: Mapping[str, str]) -> str:
    """The engine's key: from the environment, the app's options, or /data."""
    key = environ.get("API_KEY", "").strip() or _option(data)
    stored = data / KEY_FILE
    if not key and stored.exists():
        key = stored.read_text(encoding="utf-8").strip()
    if not key:
        key = _create_key(stored)
        _LOGGER.info("Created a new API key in %s", stored)
    if len(key) < MIN_KEY_LENGTH:
        raise ValueError(f"the API key needs at least {MIN_KEY_LENGTH} characters")
    return key


def _create_key(stored: Path) -> str:
    """A new key, written whole to `stored`, readable by its owner only.

    The key is written to a temporary file first, so that a start cut short
    leaves either no key or the whole key, never an empty file.
    """
    key = secrets.token_urlsafe(32)
    temporary = stored.with_name(f".{stored.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(key)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(stored)
    return key


def _option(data: Path) -> str:
    try:
        options = json.loads((data / "options.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    value = options.get("api_key") if isinstance(options, dict) else None
    return value.strip() if isinstance(value, str) else ""


def drop_privileges(user: str, key_file: Path) -> None:
    """Run as `user` from now on, when started as root, keeping its key readable."""
    if os.geteuid() != 0:
        return
    entry = pwd.getpwnam(user)
    if key_file.exists():
        os.chown(key_file, entry.pw_uid, entry.pw_gid)
    os.setgroups([])
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)


async def announce(
    session: aiohttp.ClientSession,
    token: str,
    port: int,
    key: str,
    *,
    base: str | None = None,
    attempts: int = 10,
    delay: float = 5,
) -> str | None:
    """Announce the engine to Home Assistant; the id of the discovery, or None."""
    base = base or SUPERVISOR
    headers = {"Authorization": f"Bearer {token}"}
    hostname: object = None
    for attempt in range(1, attempts + 1):
        try:
            async with session.get(f"{base}/addons/self/info", headers=headers) as response:
                response.raise_for_status()
                hostname = (await response.json())["data"]["hostname"]
            config = {"host": hostname, "port": port, "api_key": key}
            async with session.post(
                f"{base}/discovery", headers=headers, json={"service": SERVICE, "config": config}
            ) as response:
                response.raise_for_status()
                discovery = (await response.json())["data"]["uuid"]
        except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as err:
            _LOGGER.warning(
                "Announcement to Home Assistant failed (%s/%s): %s", attempt, attempts, err
            )
            if attempt < attempts:
                await asyncio.sleep(delay)
        else:
            _LOGGER.info("Announced to Home Assistant as %s:%s", hostname, port)
            return discovery
    _LOGGER.error(
        "Could not announce the engine. To add the integration by hand, set the app's API key"
        " option, restart the app, and enter that key with the URL http://%s:%s",
        hostname or "<this app's hostname>",
        port,
    )
    return None


def build(environ: Mapping[str, str]) -> tuple[web.Application, int]:
    """The application to serve, and its port."""
    data = Path(environ.get("ENGINE_DATA", "/data"))
    key = api_key(data, environ)
    drop_privileges(environ.get("ENGINE_USER", "engine"), data / KEY_FILE)
    detector, embedder = load_engine(
        Path(environ.get("ENGINE_MANIFEST", "/app/models.json")),
        Path(environ.get("ENGINE_MODELS", "/models")),
    )
    version = environ.get("ENGINE_VERSION", "0.0.0")
    app = create_app(Engine(version, detector, embedder), key)
    port = int(environ.get("ENGINE_PORT", PORT))
    token = environ.get("SUPERVISOR_TOKEN")
    if token:

        async def announced(_app: web.Application) -> AsyncIterator[None]:
            timeout = aiohttp.ClientTimeout(total=ANNOUNCE_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                task = asyncio.create_task(announce(session, token, port, key))
                yield
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        app.cleanup_ctx.append(announced)
    _LOGGER.info("Pet Recognition Engine %s, model %s", version, embedder.model)
    return app, port


def main() -> None:
    """Run the engine until it is stopped."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app, port = build(os.environ)
    # Inside the app's container; under the Supervisor the port is not published.
    # A client that leaves must not cancel its request: the analysis would go on
    # in its thread after the lock is released, next to the following one.
    web.run_app(
        app,
        host="0.0.0.0",  # noqa: S104
        port=port,
        access_log=None,
        print=None,
        handler_cancellation=False,
    )

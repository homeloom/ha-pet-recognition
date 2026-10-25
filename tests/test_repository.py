"""Checks of the repository layout that keep the integration and the app in step."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP = ROOT / "pet_recognition_engine"
MANIFEST = ROOT / "custom_components" / "pet_recognition" / "manifest.json"
# The names the Supervisor takes for the configuration of an app.
CONFIGS = ("config.json", "config.yaml", "config.yml")


def test_only_the_app_has_a_supervisor_configuration() -> None:
    # The Supervisor takes every configuration file of the repository for an
    # app, except in hidden and `rootfs` directories, so no other file may
    # use these names.
    found = [
        path.relative_to(ROOT)
        for name in CONFIGS
        for path in ROOT.rglob(name)
        if not any(
            part.startswith(".") or part == "rootfs" for part in path.relative_to(ROOT).parts
        )
    ]
    assert all(path.parent == APP.relative_to(ROOT) for path in found), found


def test_the_app_uses_only_config_yaml() -> None:
    # The version check reads config.yaml; another name would escape it.
    assert not [name for name in ("config.json", "config.yml") if (APP / name).exists()]


def test_the_app_and_the_integration_share_one_version() -> None:
    version = json.loads(MANIFEST.read_text(encoding="utf-8"))["version"]
    config = APP / "config.yaml"
    if config.exists():
        text = config.read_text(encoding="utf-8")
        match = re.search(r"""^version:\s*["']?([^"'\s]+)""", text, re.MULTILINE)
        assert match, "the app configuration has no version"
        assert match.group(1) == version

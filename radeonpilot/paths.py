"""Default locations; each can be overridden via environment for development."""

from __future__ import annotations

import os
from pathlib import Path

GROUP = "radeonpilot"


def socket_path() -> Path:
    return Path(os.environ.get("RADEONPILOT_SOCKET", "/run/radeonpilot.sock"))


def config_path() -> Path:
    return Path(os.environ.get("RADEONPILOT_CONFIG", "/etc/radeonpilot/config.json"))


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "radeonpilot"


def user_applications_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / "applications"

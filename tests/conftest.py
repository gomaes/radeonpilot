from pathlib import Path

import pytest

from radeonpilot.daemon.controller import Controller
from radeonpilot.daemon.profiles import ProfileStore
from radeonpilot.emulator import EmulatedBackend, build_tree

RX9070XT = "0000:03:00.0"
RX7900XTX = "0000:08:00.0"


@pytest.fixture
def emu_root(tmp_path: Path, monkeypatch) -> Path:
    root = build_tree(tmp_path / "sysfs")
    monkeypatch.setenv("RADEONPILOT_SYSFS_ROOT", str(root))
    monkeypatch.delenv("RADEONPILOT_EMU_FAIL", raising=False)
    return root


@pytest.fixture
def backend(emu_root) -> EmulatedBackend:
    return EmulatedBackend(emu_root)


@pytest.fixture
def controller(emu_root, backend, tmp_path) -> Controller:
    return Controller(backend, emu_root, ProfileStore(tmp_path / "etc/config.json"))

import re
import sys

import pytest

from radeonpilot import launcher
from radeonpilot.launcher import AppEntry, AppStore
from radeonpilot.sysfs import discover_gpus


def unescape_exec(value: str) -> list[str]:
    """Reference decoder for the Desktop Entry Exec key (string escapes, then quoting)."""
    value = re.sub(r"\\(.)", lambda m: {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}.get(m.group(1), "\\" + m.group(1)), value)
    args, cur, i, quoted, have = [], "", 0, False, False
    while i < len(value):
        ch = value[i]
        if quoted:
            if ch == "\\" and i + 1 < len(value) and value[i + 1] in '"`$\\':
                cur += value[i + 1]
                i += 2
                continue
            if ch == '"':
                quoted = False
            else:
                cur += ch
        elif ch == '"':
            quoted, have = True, True
        elif ch == " ":
            if cur or have:
                args.append(cur)
            cur, have = "", False
        else:
            cur += ch
        i += 1
    if cur or have:
        args.append(cur)
    return [a.replace("%%", "%") for a in args]


@pytest.fixture
def gpu(emu_root):
    return discover_gpus(emu_root)[1]  # 7900 XTX


def test_env(gpu):
    assert launcher.gpu_env(gpu) == {"DRI_PRIME": "pci-0000_08_00_0", "MESA_VK_DEVICE_SELECT": "1002:744c"}
    assert launcher.steam_launch_options(gpu) == "DRI_PRIME=pci-0000_08_00_0 MESA_VK_DEVICE_SELECT=1002:744c %command%"


@pytest.mark.parametrize("command", [
    "vkcube",
    "'/home/user/My Games/run.sh' --opt=\"a b\"",
    "sh -c 'echo $HOME `id` \\\\ 100% ; true'",
    "wine 'C:\\Games\\x.exe'",
])
def test_desktop_exec_roundtrip(gpu, command, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    app = AppEntry(name="Test ゲーム\nX", command=command, gpu=gpu.pci_address, workdir="/tmp")
    text = launcher.desktop_entry(app, gpu)
    exec_line = next(l for l in text.splitlines() if l.startswith("Exec="))
    assert unescape_exec(exec_line[5:]) == ["env", "DRI_PRIME=pci-0000_08_00_0", "MESA_VK_DEVICE_SELECT=1002:744c", *app.argv()]
    assert "Name=Test ゲーム X (Radeon RX 7900 XTX)" in text
    assert text.count("\n") == len(text.splitlines())  # no stray line breaks


def test_desktop_write_remove(gpu, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    app = AppEntry(name="Cyberpunk 2077", command="cp2077", gpu=gpu.pci_address)
    path = launcher.write_desktop(app, gpu)
    assert path.parent == tmp_path / "applications" and path.name.startswith("radeonpilot-cyberpunk-2077-")
    assert launcher.remove_desktop(app) and not path.exists()
    assert not launcher.remove_desktop(app)


def test_launch_sets_env(gpu, tmp_path):
    out = tmp_path / "env.txt"
    app = AppEntry(name="x", command=f"{sys.executable} -c \"import os; open('{out}','w').write(os.environ['DRI_PRIME']+' '+os.environ['MESA_VK_DEVICE_SELECT'])\"", gpu=gpu.pci_address)
    launcher.launch(app, gpu).wait(10)
    assert out.read_text() == "pci-0000_08_00_0 1002:744c"


def test_bad_command(gpu):
    with pytest.raises(ValueError):
        AppEntry(name="x", command="", gpu=gpu.pci_address).argv()
    with pytest.raises(ValueError):
        AppEntry(name="x", command="'unterminated", gpu=gpu.pci_address).argv()


def test_store(tmp_path):
    store = AppStore(tmp_path / "apps.json")
    assert store.load() == []
    apps = [AppEntry(name="a", command="b", gpu="0000:03:00.0")]
    store.save(apps)
    assert store.load() == apps
    (tmp_path / "apps.json").write_text("garbage")
    assert store.load() == []

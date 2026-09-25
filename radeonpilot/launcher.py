"""Run applications on a chosen GPU (DRI_PRIME / MESA_VK_DEVICE_SELECT)."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import user_applications_dir, user_config_dir
from .sysfs import GpuInfo

DESKTOP_PREFIX = "radeonpilot-"


@dataclass
class AppEntry:
    name: str
    command: str
    gpu: str  # PCI address, e.g. 0000:03:00.0
    workdir: str = ""
    icon: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def argv(self) -> list[str]:
        try:
            argv = shlex.split(self.command)
        except ValueError as exc:
            raise ValueError(f"コマンドを解釈できません: {exc}") from None
        if not argv:
            raise ValueError("コマンドが空です")
        return argv


class AppStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else user_config_dir() / "apps.json"

    def load(self) -> list[AppEntry]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []
        apps = []
        for item in raw.get("apps", []) if isinstance(raw, dict) else []:
            try:
                apps.append(AppEntry(**{k: str(v) for k, v in item.items() if k in AppEntry.__dataclass_fields__}))
            except TypeError:
                continue
        return apps

    def save(self, apps: list[AppEntry]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.write_text(json.dumps({"apps": [asdict(a) for a in apps]}, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)


def gpu_env(gpu: GpuInfo) -> dict[str, str]:
    return {"DRI_PRIME": gpu.dri_prime_id, "MESA_VK_DEVICE_SELECT": gpu.vk_device_select}


def env_prefix(gpu: GpuInfo) -> str:
    return " ".join(f"{k}={v}" for k, v in gpu_env(gpu).items())


def steam_launch_options(gpu: GpuInfo) -> str:
    return f"{env_prefix(gpu)} %command%"


def launch(app: AppEntry, gpu: GpuInfo) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(gpu_env(gpu))
    cwd = os.path.expanduser(app.workdir) if app.workdir else None
    return subprocess.Popen(
        app.argv(),
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------- .desktop

_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def desktop_exec_quote(arg: str) -> str:
    """Quote one argument for an Exec= key (Desktop Entry Specification)."""
    arg = arg.replace("%", "%%")
    if arg and not any(c in _EXEC_RESERVED for c in arg):
        return arg
    return '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'


def _escape_value(value: str) -> str:
    """Escape a string value (backslash and line breaks) for a .desktop key."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _one_line(value: str) -> str:
    return " ".join(value.split())


def gpu_label(gpu: GpuInfo) -> str:
    return gpu.name.removeprefix("AMD ")


def desktop_path(app: AppEntry) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", app.name.lower()).strip("-")[:40]
    return user_applications_dir() / f"{DESKTOP_PREFIX}{slug + '-' if slug else ''}{app.id[:8]}.desktop"


def desktop_entry(app: AppEntry, gpu: GpuInfo) -> str:
    args = ["env", *(f"{k}={v}" for k, v in gpu_env(gpu).items()), *app.argv()]
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={_escape_value(_one_line(app.name))} ({_escape_value(gpu_label(gpu))})",
        f"Comment={_escape_value(f'RadeonPilot: {gpu.name} ({gpu.pci_address}) で起動')}",
        f"Exec={_escape_value(' '.join(desktop_exec_quote(a) for a in args))}",
        f"Icon={_escape_value(app.icon) if app.icon else 'application-x-executable'}",
        "Terminal=false",
        "Categories=Utility;",
        f"X-RadeonPilot-GPU={gpu.pci_address}",
    ]
    if app.workdir:
        lines.append(f"Path={_escape_value(os.path.expanduser(app.workdir))}")
    return "\n".join(lines) + "\n"


def write_desktop(app: AppEntry, gpu: GpuInfo) -> Path:
    path = desktop_path(app)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(desktop_entry(app, gpu))
    os.chmod(tmp, 0o755)  # some desktops only trust executable launchers
    os.replace(tmp, path)
    _update_desktop_database(path.parent)
    return path


def remove_desktop(app: AppEntry) -> bool:
    path = desktop_path(app)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _update_desktop_database(path.parent)
    return True


def _update_desktop_database(directory: Path) -> None:
    tool = shutil.which("update-desktop-database")
    if tool:
        subprocess.run([tool, str(directory)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)

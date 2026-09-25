"""Persistent profiles in /etc/radeonpilot/config.json.

Layout::

    {"version": 1,
     "gpus": {"0000:03:00.0": {"device_id": "1002:7550",
                               "boot_profile": "quiet",
                               "profiles": {"quiet": {...settings...}}}}}
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

VERSION = 1


class ProfileStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.RLock()

    def load(self) -> dict:
        with self.lock:
            try:
                data = json.loads(self.path.read_text())
            except FileNotFoundError:
                return {"version": VERSION, "gpus": {}}
            except (OSError, ValueError) as exc:
                backup = self.path.with_name(f"{self.path.name}.broken-{int(time.time())}")
                log.error("config %s is unreadable (%s); moving it to %s", self.path, exc, backup)
                try:
                    os.replace(self.path, backup)
                except OSError:
                    pass
                return {"version": VERSION, "gpus": {}}
            if not isinstance(data, dict) or not isinstance(data.get("gpus"), dict):
                log.error("config %s has an unexpected layout; ignoring it", self.path)
                return {"version": VERSION, "gpus": {}}
            return data

    def save(self, data: dict) -> None:
        with self.lock:
            self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            with open(tmp, "w") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)

    def gpu_entry(self, data: dict, pci: str, device_id: str) -> dict:
        entry = data["gpus"].setdefault(pci, {"device_id": device_id, "boot_profile": None, "profiles": {}})
        entry["device_id"] = device_id
        entry.setdefault("profiles", {})
        entry.setdefault("boot_profile", None)
        return entry

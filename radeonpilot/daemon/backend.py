"""Writes to sysfs. Only a fixed set of attribute names is ever allowed."""

from __future__ import annotations

import logging
from pathlib import Path

from ..control import WRITABLE_ATTRS

log = logging.getLogger(__name__)


class SysfsBackend:
    """Writes one command per open()/write(), exactly like ``echo > attr``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()

    def write(self, path: Path, text: str) -> None:
        path = Path(path)
        if path.name not in WRITABLE_ATTRS:
            raise PermissionError(f"refusing to write non-whitelisted attribute {path}")
        resolved = path.resolve()
        if self.root / "sys" not in resolved.parents:
            raise PermissionError(f"refusing to write outside sysfs: {resolved}")
        log.info("write %s <- %r", resolved, text)
        with open(resolved, "w") as fh:
            fh.write(text + "\n")

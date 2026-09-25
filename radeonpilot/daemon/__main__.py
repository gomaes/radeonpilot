"""radeonpilot-daemon entry point (runs as root under systemd)."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from .. import __version__
from ..paths import GROUP, config_path, socket_path
from ..sysfs import sysfs_root
from .backend import SysfsBackend
from .controller import Controller
from .profiles import ProfileStore
from .server import DaemonServer

log = logging.getLogger("radeonpilot.daemon")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radeonpilot-daemon")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--socket", type=Path, default=socket_path())
    parser.add_argument("--config", type=Path, default=config_path())
    parser.add_argument("--sysfs-root", type=Path, default=sysfs_root())
    parser.add_argument("--group", default=GROUP, help="group allowed to use the socket")
    parser.add_argument("--emulate", action="store_true",
                        help="use the emulated backend on --sysfs-root (development only)")
    parser.add_argument("--no-boot-apply", action="store_true", help="do not apply boot profiles")
    parser.add_argument("--reset-all", action="store_true",
                        help="reset every GPU to driver defaults and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    sim = None
    if args.emulate:
        from ..emulator import EmulatedBackend, Simulator

        backend = EmulatedBackend(args.sysfs_root)
        if not args.reset_all:
            sim = Simulator(backend)
            sim.start()
        log.warning("EMULATION MODE: sysfs root %s", backend.root)
    else:
        if os.geteuid() != 0:
            log.error("the daemon must run as root (use --emulate for development)")
            return 1
        backend = SysfsBackend(args.sysfs_root)

    controller = Controller(backend, args.sysfs_root, ProfileStore(args.config))

    if args.reset_all:
        errors = controller.reset_all_gpus()
        for err in errors:
            log.error("%s", err)
        return 1 if errors else 0

    try:
        server = DaemonServer(args.socket, controller, args.group)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    def _shutdown(signum, _frame):
        log.info("signal %d received, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    if not args.no_boot_apply:
        threading.Thread(target=controller.apply_boot_profiles, name="boot-apply", daemon=True).start()

    log.info("radeonpilot-daemon %s listening on %s", __version__, args.socket)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        if sim:
            sim.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Unix socket server. Access: root, the daemon's own uid, and members of the radeonpilot group."""

from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import socket
import socketserver
import struct
from pathlib import Path

from .. import __version__
from ..control import ValidationError
from ..protocol import MAX_MESSAGE
from .controller import ControlError, Controller

log = logging.getLogger(__name__)


def _params(p: dict, *names: str) -> list:
    return [p.get(n) for n in names]


COMMANDS = {
    "ping": lambda c, p: {"version": __version__},
    "gpus": lambda c, p: c.gpus(),
    "state": lambda c, p: c.state(*_params(p, "gpu")),
    "set_perf_level": lambda c, p: c.set_perf_level(*_params(p, "gpu", "level")),
    "set_power_cap": lambda c, p: c.set_power_cap(*_params(p, "gpu", "watts")),
    "set_od": lambda c, p: c.set_od(*_params(p, "gpu", "values")),
    "reset_od": lambda c, p: c.reset_od(*_params(p, "gpu")),
    "set_fan_curve": lambda c, p: c.set_fan_curve(*_params(p, "gpu", "points")),
    "reset_fan_curve": lambda c, p: c.reset_fan_curve(*_params(p, "gpu")),
    "reset": lambda c, p: c.reset(*_params(p, "gpu")),
    "list_profiles": lambda c, p: c.list_profiles(*_params(p, "gpu")),
    "save_profile": lambda c, p: c.save_profile(*_params(p, "gpu", "name")),
    "delete_profile": lambda c, p: c.delete_profile(*_params(p, "gpu", "name")),
    "apply_profile": lambda c, p: c.apply_profile(*_params(p, "gpu", "name")),
    "set_boot_profile": lambda c, p: c.set_boot_profile(*_params(p, "gpu", "name")),
}


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", data)  # pid, uid, gid


class _Handler(socketserver.StreamRequestHandler):
    server: "DaemonServer"

    def handle(self) -> None:
        try:
            pid, uid, _gid = peer_credentials(self.request)
        except OSError:
            return
        if not self.server.authorized(uid):
            log.warning("denied connection from uid %d (pid %d)", uid, pid)
            self._send({"id": None, "ok": False, "kind": "denied", "error": "アクセスが拒否されました"})
            return
        while True:
            line = self.rfile.readline(MAX_MESSAGE + 1)
            if not line:
                return
            if len(line) > MAX_MESSAGE:
                self._send({"id": None, "ok": False, "kind": "validation", "error": "リクエストが大きすぎます"})
                return
            self._send(self.server.dispatch(line, uid))

    def _send(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode() + b"\n")
        self.wfile.flush()


class DaemonServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, controller: Controller, group: str | None) -> None:
        self.controller = controller
        self.gid = None
        if group:
            try:
                self.gid = grp.getgrnam(group).gr_gid
            except KeyError:
                log.warning("group %r does not exist; only root and uid %d may connect", group, os.geteuid())
        path = Path(path)
        _remove_stale_socket(path)
        old_umask = os.umask(0o117 if self.gid is not None else 0o177)
        try:
            super().__init__(str(path), _Handler)
        finally:
            os.umask(old_umask)
        if self.gid is not None:
            try:
                os.chown(path, -1, self.gid)
            except PermissionError:
                log.warning("cannot chgrp %s (not root?); access limited to uid %d", path, os.geteuid())
        self.socket_path = path

    def authorized(self, uid: int) -> bool:
        if uid in (0, os.geteuid()):
            return True
        if self.gid is None:
            return False
        try:
            pw = pwd.getpwuid(uid)
        except KeyError:
            return False
        return self.gid in os.getgrouplist(pw.pw_name, pw.pw_gid)

    def dispatch(self, line: bytes, uid: int) -> dict:
        req_id = None
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError
            req_id = req.get("id")
            cmd = req.get("cmd")
            params = req.get("params") or {}
            if cmd not in COMMANDS or not isinstance(params, dict):
                raise ValidationError(f"不明なコマンドです: {cmd!r}")
        except ValidationError as exc:
            return {"id": req_id, "ok": False, "kind": "validation", "error": str(exc)}
        except ValueError:
            return {"id": req_id, "ok": False, "kind": "validation", "error": "JSON を解釈できません"}
        if cmd not in ("ping", "gpus", "state", "list_profiles"):
            log.info("uid %d: %s %s", uid, cmd, json.dumps(params, ensure_ascii=False))
        try:
            return {"id": req_id, "ok": True, "result": COMMANDS[cmd](self.controller, params)}
        except ValidationError as exc:
            log.info("rejected: %s", exc)
            return {"id": req_id, "ok": False, "kind": "validation", "error": str(exc)}
        except ControlError as exc:
            return {"id": req_id, "ok": False, "kind": "write_failed", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - never kill the daemon on a bad request
            log.exception("internal error handling %s", cmd)
            return {"id": req_id, "ok": False, "kind": "internal", "error": f"内部エラー: {exc}"}

    def server_close(self) -> None:
        super().server_close()
        try:
            self.socket_path.unlink()
        except (OSError, AttributeError):
            pass


def _remove_stale_socket(path: Path) -> None:
    if not (path.exists() or path.is_symlink()):
        return
    if not path.is_socket():
        raise RuntimeError(f"{path} exists and is not a socket; refusing to replace it")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        path.unlink()  # stale socket from a previous run
    else:
        raise RuntimeError(f"another daemon is already listening on {path}")
    finally:
        probe.close()

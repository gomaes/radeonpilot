"""GUI <-> daemon protocol: one JSON object per line over a Unix socket.

Request:  {"id": 1, "cmd": "set_power_cap", "params": {"gpu": "0000:03:00.0", "watts": 280}}
Response: {"id": 1, "ok": true, "result": ...}
          {"id": 1, "ok": false, "kind": "validation" | "write_failed" | "denied" | "internal",
           "error": "human readable message"}
"""

from __future__ import annotations

import itertools
import json
import socket
from pathlib import Path

from .paths import socket_path

MAX_MESSAGE = 1 << 20


class DaemonError(Exception):
    def __init__(self, message: str, kind: str = "internal") -> None:
        super().__init__(message)
        self.kind = kind


class DaemonUnavailable(DaemonError):
    def __init__(self, message: str) -> None:
        super().__init__(message, "unavailable")


class DaemonClient:
    def __init__(self, path: Path | None = None, timeout: float = 15.0) -> None:
        self.path = Path(path) if path is not None else socket_path()
        self.timeout = timeout
        self._ids = itertools.count(1)

    def request(self, cmd: str, **params):
        req_id = next(self._ids)
        payload = json.dumps({"id": req_id, "cmd": cmd, "params": params}).encode() + b"\n"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            try:
                sock.connect(str(self.path))
            except FileNotFoundError:
                raise DaemonUnavailable(
                    f"デーモンのソケット {self.path} がありません。"
                    "radeonpilot-daemon サービスが起動しているか確認してください"
                    "（systemctl status radeonpilot-daemon）。"
                ) from None
            except PermissionError:
                raise DaemonUnavailable(
                    f"ソケット {self.path} へのアクセス権がありません。"
                    "ユーザーが radeonpilot グループに所属しているか確認し、"
                    "追加直後の場合は一度ログアウトして再ログインしてください。"
                ) from None
            except ConnectionRefusedError:
                raise DaemonUnavailable(
                    f"デーモンが応答しません（{self.path}）。"
                    "sudo systemctl restart radeonpilot-daemon を試してください。"
                ) from None
            sock.sendall(payload)
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_MESSAGE:
                    raise DaemonError("デーモンからの応答が大きすぎます")
        except socket.timeout:
            raise DaemonError("デーモンの応答がタイムアウトしました") from None
        except OSError as exc:
            raise DaemonUnavailable(f"デーモンとの通信に失敗しました: {exc}") from None
        finally:
            sock.close()
        if not buf:
            raise DaemonError("デーモンが接続を切断しました")
        try:
            resp = json.loads(buf)
        except ValueError:
            raise DaemonError("デーモンからの応答を解釈できません") from None
        if not resp.get("ok"):
            raise DaemonError(resp.get("error", "不明なエラー"), resp.get("kind", "internal"))
        return resp.get("result")

    def available(self) -> tuple[bool, str | None]:
        try:
            self.request("ping")
            return True, None
        except DaemonError as exc:
            return False, str(exc)

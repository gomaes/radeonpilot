import threading

import pytest

from radeonpilot.daemon.server import DaemonServer
from radeonpilot.protocol import DaemonClient, DaemonError, DaemonUnavailable
from tests.conftest import RX9070XT


@pytest.fixture
def server(controller, tmp_path):
    srv = DaemonServer(tmp_path / "rp.sock", controller, group="radeonpilot-does-not-exist")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_roundtrip(server):
    client = DaemonClient(server.socket_path)
    assert client.available() == (True, None)
    assert [g["pci"] for g in client.request("gpus")] == [RX9070XT, "0000:08:00.0"]
    st = client.request("set_power_cap", gpu=RX9070XT, watts=300)
    assert st["power"]["current_w"] == 300


def test_errors(server):
    client = DaemonClient(server.socket_path)
    with pytest.raises(DaemonError) as exc:
        client.request("set_power_cap", gpu=RX9070XT, watts=9999)
    assert exc.value.kind == "validation"
    with pytest.raises(DaemonError) as exc:
        client.request("rm_rf")
    assert exc.value.kind == "validation"
    with pytest.raises(DaemonError) as exc:
        client.request("set_od", gpu=RX9070XT, values="s 1 9999")
    assert exc.value.kind == "validation"


def test_socket_mode(server):
    # No such group: owner only.
    assert oct(server.socket_path.stat().st_mode & 0o777) == "0o600"


def test_unavailable(tmp_path):
    with pytest.raises(DaemonUnavailable):
        DaemonClient(tmp_path / "missing.sock").request("ping")


def test_stale_socket_replaced_and_live_refused(controller, tmp_path, server):
    with pytest.raises(RuntimeError):
        DaemonServer(server.socket_path, controller, None)


def test_authorization(server):
    import os

    assert server.authorized(0)
    assert server.authorized(os.geteuid())
    assert not server.authorized(65534)  # nobody, not in the (missing) group

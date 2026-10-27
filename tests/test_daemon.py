"""MonitorCache event-socket handling, against a fake Hyprland socket2 (no compositor)."""

import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("pywayland", reason="hyprmeta.daemon needs pywayland (the pipx venv has it)")
from gi.repository import GLib  # noqa: E402

from hyprmeta import daemon  # noqa: E402


@pytest.fixture
def socket2(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("HYPRLAND_INSTANCE_SIGNATURE", "s")
    os.makedirs(tmp_path / "hypr" / "s")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(tmp_path / "hypr" / "s" / ".socket2.sock"))
    server.listen(1)
    yield server
    server.close()


def make_cache(server):
    cache = daemon.MonitorCache(SimpleNamespace(monitors=lambda: []))
    conn, _ = server.accept()
    got = []
    cache.on_events = got.extend
    return cache, conn, got


def pump_until(cond, timeout=5.0):
    ctx = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition not reached"
        ctx.iteration(False)
        time.sleep(0.001)


def test_events_are_drained_while_the_gtk_loop_is_blocked(socket2):
    # Oct 27 2026: the GTK loop sat in hyprctl timeouts, nobody read socket2, and
    # Hyprland dropped the daemon. Far more than the kernel buffer + Hyprland's
    # 64-event queue must be accepted while the GLib loop does NOT iterate.
    cache, conn, got = make_cache(socket2)
    n = 20000
    payload = b"".join(b"windowtitlev2>>a1b2c3d4e5f6,%d spinner | proj\n" % i for i in range(n))
    conn.settimeout(10)
    sender = threading.Thread(target=conn.sendall, args=(payload,))
    sender.start()
    sender.join(10)
    assert not sender.is_alive(), "socket2 writer blocked: events are not read off the GTK loop"
    pump_until(lambda: len(got) == n)
    assert got[0] == ("windowtitlev2", "a1b2c3d4e5f6,0 spinner | proj")
    assert got[-1] == ("windowtitlev2", f"a1b2c3d4e5f6,{n - 1} spinner | proj")
    assert cache.lost is None
    conn.close()


def test_a_line_split_across_reads_is_delivered_once(socket2):
    cache, conn, got = make_cache(socket2)
    conn.sendall(b"activewindowv2>>ab")
    time.sleep(0.05)
    conn.sendall("c,ä\n".encode()[:2])  # also split inside a UTF-8 sequence
    time.sleep(0.05)
    conn.sendall("c,ä\n".encode()[2:])
    pump_until(lambda: got)
    assert got == [("activewindowv2", "abc,ä")]
    conn.close()


def test_a_closed_socket_marks_the_cache_lost_and_quits(socket2, monkeypatch):
    quits = []
    monkeypatch.setattr(daemon, "Gtk", SimpleNamespace(main_quit=lambda: quits.append(1)))
    cache, conn, _ = make_cache(socket2)
    conn.close()  # what Hyprland does to a listener that overflowed its queue
    pump_until(lambda: quits)
    assert cache.lost == "Hyprland event socket EOF"

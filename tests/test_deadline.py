"""A dripping server can't hold a worker: socket reads obey a wall clock / cancel."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from bot.utils.deadline import DeadlineExceeded, deadline


def _drip_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def serve():
        conn, _ = srv.accept()
        while not stop.is_set():
            try:
                conn.sendall(b"x")
            except OSError:
                break
            time.sleep(0.05)
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv, stop


def _read_forever(port):
    c = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        while True:
            c.recv(1)  # each read succeeds: a per-read timeout never fires
    finally:
        c.close()


def test_wall_clock_stops_a_dripping_read():
    srv, stop = _drip_server()
    t0 = time.monotonic()
    with pytest.raises(DeadlineExceeded):
        with deadline(0.4):
            _read_forever(srv.getsockname()[1])
    assert time.monotonic() - t0 < 2
    stop.set()
    srv.close()


def test_cancel_stops_a_read_at_once():
    srv, stop = _drip_server()
    ev = threading.Event()
    threading.Timer(0.2, ev.set).start()
    with pytest.raises(DeadlineExceeded, match="Cancelled"):
        with deadline(60, cancel=ev):
            _read_forever(srv.getsockname()[1])
    stop.set()
    srv.close()


def test_other_threads_are_untouched():
    srv, stop = _drip_server()
    got = []

    def other():
        c = socket.create_connection(("127.0.0.1", srv.getsockname()[1]), timeout=5)
        got.append(c.recv(1))
        c.close()

    with deadline(0):  # already expired — but only for THIS thread
        t = threading.Thread(target=other)
        t.start()
        t.join(5)
    assert got == [b"x"]
    stop.set()
    srv.close()

"""Tests for the DVRIP connection supervisor and the socket liveness probe.

Why these exist
---------------
The doorbell went silent three times (37 days, 24 days, 13.7 days), each
time recovering only when a human restarted the Scrypted plugin worker —
even though the plugin already had a reconnect loop AND a stall watchdog.
One of those safety nets, `remote.connected()`, was reading a bool that
only our own close() could ever flip: a check that could not fail. The
others could not be exonerated after the fact, because Scrypted keeps
plugin console output in memory and the restart destroyed it.

So the supervisor is deliberately theory-free: it restarts the connection
when the thread stops making progress, whatever the reason. These tests
pin that behaviour, and — importantly — each has a CONTROL that must NOT
trigger, so a supervisor that simply restarts everything all the time
would fail them.

Run: python3 tests/test_dvrip_supervisor.py
"""
import os
import socket
import sys
import threading
import time
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), 'src')
sys.path.insert(0, _SRC)
sys.path.insert(0, os.path.join(_SRC, 'lorex', 'DahuaConsole'))

# dvrip_client imports DahuaFunctions from the vendored DahuaConsole,
# which drags in the whole networking stack. The supervisor logic under
# test never constructs one, so stub the module out.
if 'dahua' not in sys.modules:
    _stub = types.ModuleType('dahua')

    class DahuaFunctions:  # noqa: D401 - stub
        def __init__(self, **kwargs):
            pass

    _stub.DahuaFunctions = DahuaFunctions
    sys.modules['dahua'] = _stub

from lorex.dvrip_client import DvripClient  # noqa: E402
import pwn  # noqa: E402  (the vendored shim)


class _Client(DvripClient):
    """DvripClient with the real supervisor but a scripted _run()."""

    SUPERVISOR_TICK_SEC = 0.05
    HEARTBEAT_STALL_SEC = 0.4

    def __init__(self, mode):
        super().__init__(host='192.0.2.1', port=35000, user='u',
                         password='p', on_event=lambda e: None)
        self.mode = mode
        self.runs = []

    def _run(self, gen=0):
        self.runs.append(gen)
        if self.mode == 'wedged':
            # Simulates the observed failure: thread alive, socket idle,
            # no progress, forever. A blocking C call would look like
            # this from the outside.
            while not self._stop.is_set():
                time.sleep(0.02)
            return
        # healthy: same loop, but stamping progress the way the real
        # inner loop does.
        while not self._stop.is_set():
            if not self._is_current(gen):
                return
            self._beat()
            time.sleep(0.02)


class SupervisorTests(unittest.TestCase):

    def test_wedged_thread_is_rebuilt(self):
        c = _Client('wedged')
        c.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and c._restarts < 1:
                time.sleep(0.05)
            self.assertGreaterEqual(
                c._restarts, 1,
                'supervisor never rebuilt a thread that stopped '
                'making progress')
            self.assertGreaterEqual(
                len(c.runs), 2,
                'a replacement connection thread was never started')
            self.assertEqual(c.runs[-1], c._generation,
                             'replacement thread did not get the current '
                             'generation')
        finally:
            c.stop()

    def test_healthy_thread_is_left_alone(self):
        """CONTROL. Without this, a supervisor that restarts
        unconditionally would pass the test above."""
        c = _Client('healthy')
        c.start()
        try:
            time.sleep(1.5)  # ~30 supervisor ticks, ~4x the stall window
            self.assertEqual(
                c._restarts, 0,
                'supervisor restarted a thread that was reporting '
                'progress the whole time')
            self.assertEqual(len(c.runs), 1)
        finally:
            c.stop()

    def test_abandoned_thread_cannot_stamp_the_heartbeat(self):
        """A superseded thread that wakes up must not mask a wedged
        current thread — that would recreate the original silent
        failure."""
        c = _Client('healthy')
        # Keep the supervisor out of it: it would notice the stale
        # heartbeat and restart, stamping a fresh beat and hiding the
        # very thing under test.
        c.HEARTBEAT_STALL_SEC = 3600
        c.start()
        try:
            time.sleep(0.2)
            c._generation += 1          # supersede the running thread
            old_beat = c._since_beat()
            time.sleep(0.5)
            self.assertGreater(
                c._since_beat(), old_beat,
                'the superseded thread kept stamping the heartbeat')
        finally:
            c.stop()

    def test_supervisor_survives_a_raising_callback(self):
        """on_state is consumer code; if it throws, the last safety net
        must not die with it."""
        c = _Client('wedged')

        def boom(_connected):
            raise RuntimeError('consumer exploded')

        c.on_state = boom
        c.start()
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and c._restarts < 2:
                time.sleep(0.05)
            self.assertGreaterEqual(
                c._restarts, 2,
                'supervisor stopped after a callback raised')
        finally:
            c.stop()


class SocketLivenessTests(unittest.TestCase):
    """`remote.connected()` used to return a bool only our own close()
    could change. These pin that it now reflects the actual peer."""

    def _wrap(self, sock):
        r = pwn.remote.__new__(pwn.remote)
        r.sock = sock
        r._connected = True
        return r

    def test_reports_false_once_peer_closes(self):
        a, b = socket.socketpair()
        try:
            r = self._wrap(a)
            self.assertTrue(r.connected(), 'live socket reported dead')
            b.close()
            time.sleep(0.05)
            self.assertFalse(
                r.connected(),
                'socket still reported connected after the peer closed '
                '— this is the original defect')
        finally:
            a.close()

    def test_open_idle_socket_still_reports_true(self):
        """CONTROL. An idle-but-healthy connection is the common case;
        reporting it dead would cause a reconnect storm."""
        a, b = socket.socketpair()
        try:
            r = self._wrap(a)
            for _ in range(5):
                self.assertTrue(r.connected())
                time.sleep(0.02)
        finally:
            a.close()
            b.close()

    def test_pending_data_is_not_consumed(self):
        """The probe peeks; it must not eat a byte of a real packet."""
        a, b = socket.socketpair()
        try:
            r = self._wrap(a)
            b.sendall(b'HELLO')
            time.sleep(0.05)
            self.assertTrue(r.connected())
            self.assertEqual(a.recv(5), b'HELLO',
                             'liveness probe consumed real payload bytes')
        finally:
            a.close()
            b.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)

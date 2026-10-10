"""DVRIP client — replaces bridge.py's LorexBridge + run_connection loop.

Holds a long-lived DVRIP connection to a Lorex/Dahua/Skywatch doorbell,
subscribes to the eventManager, and forwards each `client.notifyEventStream`
event to a callback. No HTTP, no SSE — the consumer (LorexDoorbellCamera)
is in the same process.

DahuaConsole is blocking-socket-based with internal keepalive threads, so
this module wraps the whole thing in one outer thread and bridges back to
the asyncio loop via a callback the consumer marshals into the loop.
"""
import argparse
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

# DahuaConsole vendored at lorex/DahuaConsole/, on sys.path via main.py
from dahua import DahuaFunctions  # noqa: E402

log = logging.getLogger('lorex.dvrip')


def _make_dargs(host: str, port: int, user: str, password: str,
                proto: str = 'dvrip', debug: bool = False):
    """DahuaFunctions expects an argparse.Namespace with specific fields."""
    ns = argparse.Namespace()
    ns.rhost = host
    ns.rport = port
    ns.proto = proto
    ns.auth = f'{user}:{password}'
    ns.ssl = False
    ns.relay = None
    ns.logon = 'default'
    ns.events = True
    ns.multihost = False
    ns.save = False
    ns.test = False
    ns.dump = False
    ns.dump_argv = None
    ns.discover = None
    ns.timeout = 5
    ns.debug = debug
    ns.calls = False
    ns.force = False
    return ns


class _LorexBridge(DahuaFunctions):
    """Thin DahuaFunctions subclass that re-routes event notifications
    into our callback instead of DahuaConsole's internal logging.

    Also tracks `last_inbound_ts` — the monotonic time of the most recent
    packet received from the doorbell. DvripClient's stall watchdog reads
    this to decide whether the connection has silently died.
    """

    def __init__(self, on_event: Callable[[Dict[str, Any]], None], **kwargs):
        self._on_event = on_event
        # Set BEFORE super().__init__ so any inbound traffic during the
        # base class's connect path can update it without AttributeError.
        self.last_inbound_ts: float = time.monotonic()
        super().__init__(**kwargs)

    def _check_for_keepalive(self, dh_data):
        # Every inbound packet — keepalive pong, event notification, error
        # response, anything — passes through this method. Updating
        # `last_inbound_ts` here gives us a single source of truth for
        # connection liveness that doesn't depend on which event types
        # the doorbell happens to be emitting.
        self.last_inbound_ts = time.monotonic()
        return super()._check_for_keepalive(dh_data)

    def client_notify(self, dh_data):
        # Defensive double-tap: if super()._check_for_keepalive's
        # AttributeError fallback path routes data here without going
        # through our override above, still update the timestamp.
        self.last_inbound_ts = time.monotonic()

        try:
            payload = json.loads(dh_data) if isinstance(dh_data, str) else dh_data
        except Exception:
            return super().client_notify(dh_data)

        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get('method') != 'client.notifyEventStream':
                continue
            for ev in item.get('params', {}).get('eventList', []) or []:
                evt = {
                    'host':   self.rhost,
                    'code':   ev.get('Code'),
                    'action': ev.get('Action'),
                    'index':  ev.get('Index'),
                    'data':   ev.get('Data'),
                    'utc':    ev.get('UTC'),
                    'ts':     time.time(),
                }
                log.info('event %s action=%s index=%s',
                         evt['code'], evt['action'], evt['index'])
                try:
                    self._on_event(evt)
                except Exception as e:
                    log.exception('on_event callback raised: %r', e)

        try:
            return super().client_notify(dh_data)
        except Exception as e:
            log.debug('super().client_notify raised (ignored): %r', e)


class DvripClient:
    """Owns the connection thread + reconnect/backoff loop.

    Callbacks are invoked from the connection thread; the consumer is
    responsible for marshaling state changes back to its event loop.
    """

    def __init__(self, host: str, port: int, user: str, password: str,
                 on_event: Callable[[Dict[str, Any]], None],
                 on_state: Optional[Callable[[bool], None]] = None,
                 proto: str = 'dvrip'):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.proto = proto
        self.on_event = on_event
        self.on_state = on_state or (lambda _connected: None)

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dh: Optional[_LorexBridge] = None

        # Supervisor state. `_heartbeat` is stamped by _run() on every
        # pass of its inner loop AND at each stage of the outer loop, so
        # a stale heartbeat means the connection thread itself is stuck
        # or dead — regardless of WHICH blocking call swallowed it.
        self._heartbeat: float = time.monotonic()
        self._heartbeat_lock = threading.Lock()
        self._supervisor: Optional[threading.Thread] = None
        self._restarts: int = 0
        # Generation guard. When the supervisor abandons a wedged
        # connection thread it cannot force that thread to exit — a
        # thread stuck in a blocking C call ignores everything. So the
        # old thread may wake up LATER, inside its own finally block,
        # and would happily null out `self._dh` belonging to the fresh
        # connection. Every thread carries the generation it was born
        # in and only touches shared state while it is still current.
        self._generation: int = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._beat()
        self._thread = threading.Thread(
            target=self._run, args=(self._generation,),
            name=f'dvrip-{self.host}', daemon=True)
        self._thread.start()
        if self._supervisor is None:
            self._supervisor = threading.Thread(
                target=self._supervise, name=f'dvrip-sup-{self.host}',
                daemon=True)
            self._supervisor.start()

    # ===== Liveness =====

    def _beat(self) -> None:
        with self._heartbeat_lock:
            self._heartbeat = time.monotonic()

    def _since_beat(self) -> float:
        with self._heartbeat_lock:
            return time.monotonic() - self._heartbeat

    def _force_teardown(self) -> None:
        """Break the connection thread out of whatever it is blocked on,
        from OUTSIDE that thread. Closing the socket is what actually
        unsticks a blocking recv; setting `terminate` alone does not."""
        dh = self._dh
        if dh is None:
            return
        try:
            dh.terminate = True
        except Exception:
            pass
        try:
            if dh.remote is not None:
                dh.remote.close()
        except Exception:
            pass
        try:
            sock = getattr(getattr(dh, 'remote', None), 'sock', None)
            if sock is not None:
                sock.close()
        except Exception:
            pass

    @staticmethod
    def _bounded_cleanup(dh, timeout: float = 10.0) -> None:
        """Run DahuaConsole's cleanup() with a wall clock on it.

        cleanup() issues RPCs over the socket it is tearing down. On a
        zombie connection that call can block indefinitely, which
        silently parks the whole reconnect loop — it looks identical to
        a healthy idle thread from outside.
        """
        done = threading.Event()

        def _worker():
            try:
                dh.cleanup()
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=_worker, name='dvrip-cleanup',
                         daemon=True).start()
        if not done.wait(timeout=timeout):
            log.warning('cleanup() did not finish in %.0fs — abandoning it',
                        timeout)

    def _supervise(self) -> None:
        """Outer watchdog. Deliberately does NOT trust any of the inner
        health checks — it judges only on whether _run() is still making
        progress, so it recovers from causes we have not diagnosed.

        History that earned this: the inner loop already had a stall
        watchdog and a socket-liveness check, and the doorbell still went
        silent for 37, 24 and 13.7 days on three separate occasions,
        recovering only when a human restarted the plugin worker. One of
        those checks (`remote.connected()`) was reading a flag that could
        never go false; the others could not be exonerated after the
        fact. So the rule here is: if no progress, rebuild, no theory
        required.
        """
        while not self._stop.wait(timeout=self.SUPERVISOR_TICK_SEC):
            try:
                stale = self._since_beat()
                if stale <= self.HEARTBEAT_STALL_SEC:
                    continue

                thread = self._thread
                alive = thread is not None and thread.is_alive()
                self._restarts += 1
                log.error(
                    'SUPERVISOR: connection thread made no progress for '
                    '%.0fs (alive=%s) — forcing rebuild #%d',
                    stale, alive, self._restarts)

                # Unstick it from outside, then abandon it. A thread
                # wedged in a blocking C call cannot be joined reliably,
                # so we do not stake recovery on it exiting: it is a
                # daemon thread and _run's own loop is generation-safe.
                self._force_teardown()
                # Deliberately NOT joined. A thread wedged in a blocking
                # call will not honour a join, and waiting on it would
                # delay the replacement by exactly as long as the join —
                # while proving nothing. The generation guard makes
                # abandoning it safe: it can no longer touch shared
                # state, and it is a daemon thread.
                if thread is not None and thread.is_alive():
                    log.warning('SUPERVISOR: abandoning wedged thread %s',
                                thread.name)

                try:
                    self.on_state(False)
                except Exception:
                    pass

                self._generation += 1
                self._dh = None
                self._beat()
                self._thread = threading.Thread(
                    target=self._run, args=(self._generation,),
                    name=f'dvrip-{self.host}-r{self._restarts}',
                    daemon=True)
                self._thread.start()
                log.info('SUPERVISOR: fresh connection thread started')
            except Exception as e:
                # The supervisor must never die — it is the last net.
                log.exception('SUPERVISOR tick raised (continuing): %r', e)

    def stop(self) -> None:
        self._stop.set()
        sup = self._supervisor
        self._supervisor = None
        if self._dh is not None:
            try:
                self._dh.terminate = True
                self._dh.cleanup()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if sup is not None:
            sup.join(timeout=self.SUPERVISOR_TICK_SEC + 5)
        self._dh = None

    # Force a reconnect if no inbound packet has arrived in this many
    # seconds. The doorbell normally sends keepalive pongs every ~30-60s
    # (whatever it advertises as `keepAliveInterval` after login), plus
    # frequent event traffic; a 2-minute silence is decisive evidence
    # that the connection has gone zombie.
    #
    # Why this exists: DahuaConsole's _p2p_keepalive thread is started via
    # `_thread.start_new_thread()` (a low-level thread with no excepthook).
    # Its main RPC loop only catches `requests.exceptions.RequestException`
    # and `EOFError` — anything else (e.g. ConnectionResetError during a
    # WiFi roam) kills the thread silently without setting `self.terminate`.
    # The outer loop then spins forever in `time.sleep(1)` thinking the
    # connection is alive. This watchdog is what actually unsticks us.
    STALL_TIMEOUT_SEC = 120

    # Outer supervisor. HEARTBEAT_STALL_SEC must be comfortably larger
    # than STALL_TIMEOUT_SEC plus the longest backoff (60s) plus the
    # connect/attach path, so the supervisor only fires when the inner
    # loop has genuinely stopped making progress rather than racing a
    # reconnect that is already under way.
    SUPERVISOR_TICK_SEC = 15
    HEARTBEAT_STALL_SEC = 300

    def _is_current(self, gen: int) -> bool:
        return gen == self._generation and not self._stop.is_set()

    def _run(self, gen: int = 0) -> None:
        backoff = 5
        while not self._stop.is_set():
            if not self._is_current(gen):
                log.info('connection thread gen=%d superseded; exiting', gen)
                return
            self._beat()
            dargs = _make_dargs(self.host, self.port, self.user,
                                self.password, self.proto)
            stall_reason: Optional[str] = None
            try:
                dh = _LorexBridge(
                    on_event=self.on_event,
                    rhost=self.host, rport=self.port, proto=self.proto,
                    events=True, ssl=False, relay_host=None,
                    timeout=10, udp_server=True, dargs=dargs,
                )
                if not self._is_current(gen):
                    try:
                        dh.remote.close()
                    except Exception:
                        pass
                    return
                self._dh = dh
                log.info('connecting to %s:%s (proto=%s)',
                         self.host, self.port, self.proto)
                self._beat()
                ok = self._dh.dh_connect(
                    username=self.user, password=self.password,
                    logon='default')
                self._beat()
                if not ok:
                    raise RuntimeError('dh_connect returned falsy')
                log.info('logged in; attaching eventManager')
                attached = self._dh.instance_service(
                    'eventManager',
                    attach_params={'codes': ['All']},
                    start=True)
                # dh_connect() with events enabled already instantiates + attaches
                # eventManager itself; the second call then returns False only because
                # it is already in instance_serviceDB. That is success, not failure.
                if not attached and 'eventManager' not in getattr(
                        self._dh, 'instance_serviceDB', {}):
                    # instance_service returns False (no exception) when the attach fails.
                    # Ignoring that is how the B241AJC ran a month with zero events:
                    # connected, keepalives fine, never subscribed.
                    raise RuntimeError('eventManager attach failed')
                self.on_state(True)
                backoff = 5
                # Reset liveness clock on (re)attach — we want a full grace
                # period before the watchdog can fire on a fresh connection.
                self._dh.last_inbound_ts = time.monotonic()
                log.info('eventManager attached; stall watchdog armed '
                         '(threshold=%ds)', self.STALL_TIMEOUT_SEC)

                self._beat()

                while not self._stop.is_set():
                    time.sleep(1)
                    # An abandoned thread must never stamp the
                    # heartbeat — that would mask a wedged CURRENT
                    # thread and re-create the exact silent failure
                    # this supervisor exists to catch.
                    if not self._is_current(gen):
                        return
                    # Progress stamp for the outer supervisor. This is
                    # the ONLY thing it reads, so it stays at the top of
                    # the loop, before any check that might raise.
                    self._beat()
                    # 1) DahuaConsole's keepalive thread already detected
                    #    a real disconnect and tore itself down.
                    if self._dh.terminate:
                        stall_reason = 'DahuaConsole signaled terminate'
                        break
                    # 2) Underlying TCP socket is gone. The keepalive thread
                    #    may have died silently without setting terminate,
                    #    but the OS still knows the socket is dead.
                    try:
                        if not self._dh.remote.connected():
                            stall_reason = 'TCP socket reports not connected'
                            break
                    except Exception as e:
                        stall_reason = f'remote.connected() raised: {e!r}'
                        break
                    # 3) Stall: keepalive thread silently died but the
                    #    socket is still in ESTABLISHED state from the OS's
                    #    point of view. No inbound packets for too long.
                    silence = time.monotonic() - self._dh.last_inbound_ts
                    if silence > self.STALL_TIMEOUT_SEC:
                        stall_reason = (f'no inbound traffic for {silence:.0f}s '
                                        f'(>{self.STALL_TIMEOUT_SEC}s threshold)')
                        break
            except Exception as e:
                log.error('connection failed: %r', e)
            finally:
                # Nothing in here may raise. An exception escaping a
                # finally kills the connection thread outright, and a
                # dead thread is precisely the failure mode we cannot
                # see from the outside. (`on_state` was previously
                # called bare here.)
                try:
                    dh = self._dh
                    if stall_reason is not None:
                        log.warning('forcing reconnect: %s', stall_reason)
                        # Best-effort hard tear-down so the cleanup path
                        # below doesn't try to RPC over a dead/zombie
                        # socket and block on it.
                        try:
                            if dh is not None:
                                dh.terminate = True
                        except Exception:
                            pass
                        try:
                            if dh is not None and dh.remote is not None:
                                dh.remote.close()
                        except Exception:
                            pass
                    try:
                        self.on_state(False)
                    except Exception:
                        log.exception('on_state(False) raised (ignored)')
                    # cleanup() talks to the device and has no timeout of
                    # its own, so it can block forever on a zombie
                    # socket. Bound it: run it in a throwaway thread and
                    # walk away if it does not finish.
                    if dh is not None:
                        self._bounded_cleanup(dh)
                    if self._is_current(gen):
                        self._dh = None
                except Exception:
                    log.exception('teardown raised (ignored)')
                finally:
                    self._beat()
            if self._stop.is_set():
                break
            log.info('reconnecting in %ds', backoff)
            self._stop.wait(timeout=backoff)
            backoff = min(backoff * 2, 60)

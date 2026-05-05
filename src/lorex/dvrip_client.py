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

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f'dvrip-{self.host}', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._dh is not None:
            try:
                self._dh.terminate = True
                self._dh.cleanup()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
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

    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            dargs = _make_dargs(self.host, self.port, self.user,
                                self.password, self.proto)
            stall_reason: Optional[str] = None
            try:
                self._dh = _LorexBridge(
                    on_event=self.on_event,
                    rhost=self.host, rport=self.port, proto=self.proto,
                    events=True, ssl=False, relay_host=None,
                    timeout=10, udp_server=True, dargs=dargs,
                )
                log.info('connecting to %s:%s (proto=%s)',
                         self.host, self.port, self.proto)
                ok = self._dh.dh_connect(
                    username=self.user, password=self.password,
                    logon='default')
                if not ok:
                    raise RuntimeError('dh_connect returned falsy')
                log.info('logged in; attaching eventManager')
                self._dh.instance_service(
                    'eventManager',
                    attach_params={'codes': ['All']},
                    start=True)
                self.on_state(True)
                backoff = 5
                # Reset liveness clock on (re)attach — we want a full grace
                # period before the watchdog can fire on a fresh connection.
                self._dh.last_inbound_ts = time.monotonic()
                log.info('eventManager attached; stall watchdog armed '
                         '(threshold=%ds)', self.STALL_TIMEOUT_SEC)

                while not self._stop.is_set():
                    time.sleep(1)
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
                if stall_reason is not None:
                    log.warning('forcing reconnect: %s', stall_reason)
                    # Best-effort hard tear-down so the cleanup path below
                    # doesn't try to RPC over a dead/zombie socket and
                    # block on it.
                    try:
                        if self._dh is not None:
                            self._dh.terminate = True
                    except Exception:
                        pass
                    try:
                        if self._dh is not None and self._dh.remote is not None:
                            self._dh.remote.close()
                    except Exception:
                        pass
                self.on_state(False)
                try:
                    if self._dh is not None:
                        self._dh.cleanup()
                except Exception:
                    pass
                self._dh = None
            if self._stop.is_set():
                break
            log.info('reconnecting in %ds', backoff)
            self._stop.wait(timeout=backoff)
            backoff = min(backoff * 2, 60)

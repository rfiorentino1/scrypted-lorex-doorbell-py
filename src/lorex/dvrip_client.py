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
    into our callback instead of DahuaConsole's internal logging."""

    def __init__(self, on_event: Callable[[Dict[str, Any]], None], **kwargs):
        self._on_event = on_event
        super().__init__(**kwargs)

    def client_notify(self, dh_data):
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

    def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            dargs = _make_dargs(self.host, self.port, self.user,
                                self.password, self.proto)
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
                while not self._stop.is_set() and not self._dh.terminate:
                    time.sleep(1)
            except Exception as e:
                log.error('connection failed: %r', e)
            finally:
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

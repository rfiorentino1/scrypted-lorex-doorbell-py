"""Route stdlib logging into Scrypted's per-plugin console.

Scrypted captures `print()` from the plugin worker; stdlib `logging`
goes to stderr by default and ends up in container-level docker logs,
not the plugin's Console tab in the UI. We install a handler that
forwards each LogRecord to print(), so DahuaConsole and our own
`log.info(...)` calls show up where you can see them.
"""
import logging
import sys


class _ScryptedConsoleHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            # print() output is captured by Scrypted's plugin worker and
            # routed to the Console tab. Use stderr so it's flushed eagerly.
            print(msg, file=sys.stderr, flush=True)
        except Exception:
            self.handleError(record)


def install_logging_bridge(level: int = logging.INFO) -> None:
    """Install once at plugin startup. Idempotent."""
    root = logging.getLogger()
    # Skip if our handler is already attached.
    for h in root.handlers:
        if isinstance(h, _ScryptedConsoleHandler):
            return
    h = _ScryptedConsoleHandler()
    h.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s [%(name)s] %(message)s',
    ))
    h.setLevel(level)
    root.addHandler(h)
    root.setLevel(level)

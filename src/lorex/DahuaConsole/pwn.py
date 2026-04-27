"""Minimal `pwn` shim — replaces the pwntools dependency for DahuaConsole.

DahuaConsole's `utils.py` does `from pwn import *` for two things:
  1. Byte pack/unpack helpers (p8/p16/p32/p64/u8/u16/u32/u64), and
  2. Implicit re-exports of stdlib modules that pwntools imports at top
     level (most importantly `threading`, used in net.py for Event/Lock).

Pulling in the entire pwntools package for that drags in cffi 2.0,
which conflicts with the system's `_cffi_backend` 1.16 inside Scrypted's
plugin worker, breaks plugin load, and adds ~50 MB of unused
dependencies (paramiko, capstone, unicorn, etc.).

This shim provides exactly the API surface DahuaConsole uses. Default
endianness on the byte helpers is little (matches pwntools default when
context.endian isn't set).

This file lives inside `DahuaConsole/` and is found before any pip-
installed pwn package because `main.py` puts that directory at the
front of `sys.path`.
"""
import logging as _logging
import select as _select
import struct as _struct

# Re-exports — pwntools' `from pwn import *` brings these stdlib modules
# into the importer's namespace. DahuaConsole expects them to be
# implicitly available everywhere it does `from utils import *`. List
# matches what pwntools re-exports from its toplevel; we don't try to
# trim because mistakes there mean another iteration cycle.
import base64
import binascii
import codecs
import collections
import copy
import datetime
import errno
import functools
import hashlib
import hmac
import io
import ipaddress
import itertools
import json
import logging as logging_stdlib
import math
import os
import os.path
import pickle
import platform
import random
import re
import shlex
import shutil
import signal
import socket
import string
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import types
import typing
import urllib
import uuid
import warnings
import weakref


# pwntools exposes a `log` singleton (pwnlib.log.Logger) at module level.
# DahuaConsole uses log.info/warning/error/success/failure/progress and
# expects them to print colored output. We map them to stdlib logging
# (which is then forwarded to the Scrypted plugin console by the
# logging_bridge handler installed in main.py).
class _PwnLogShim:
    _logger = _logging.getLogger('pwn')

    def info(self, msg, *args, **kw):    self._logger.info(msg, *args)
    def warning(self, msg, *args, **kw): self._logger.warning(msg, *args)
    def error(self, msg, *args, **kw):   self._logger.error(msg, *args)
    def success(self, msg, *args, **kw): self._logger.info('[+] ' + str(msg), *args)
    def failure(self, msg, *args, **kw): self._logger.warning('[-] ' + str(msg), *args)

    # pwntools' log.progress returns a context manager / progress object
    # with .status/.success/.failure methods. DahuaConsole calls
    # log.progress(...) and may or may not chain. Return a minimal stub.
    def progress(self, msg, *args, **kw):
        self._logger.info(str(msg), *args)
        return _PwnProgressShim(self._logger)


class _PwnProgressShim:
    def __init__(self, logger): self._logger = logger
    def status(self, msg, *args, **kw):  self._logger.info(str(msg), *args)
    def success(self, msg, *args, **kw): self._logger.info('[+] ' + str(msg), *args)
    def failure(self, msg, *args, **kw): self._logger.warning('[-] ' + str(msg), *args)


log = _PwnLogShim()


# pwntools defines its own exception hierarchy. DahuaConsole's
# connection.py and net.py both `except PwnlibException` to swallow
# pwntools errors that real pwntools would raise. We never run real
# pwntools code, so this except will never match anything — but Python
# still needs the name resolvable at the except site.
class PwnlibException(Exception):
    pass


# pwntools provides a `remote(host, port)` class — a "tube" abstraction
# over a TCP socket with send/recv/close. DahuaConsole uses it as
# `self.remote = remote(host, port, ssl=..., timeout=...)` and then
# calls a small subset of methods (send, recv, close, connected,
# can_recv, plus accessing .sock). Implement the minimum API surface
# that DahuaConsole actually exercises, backed by a stdlib socket.
class remote:
    def __init__(self, host, port, ssl=False, timeout=None, **kwargs):
        if ssl:
            raise NotImplementedError(
                'remote() shim: SSL not implemented (DahuaConsole calls '
                "this with ssl=False for the doorbell's DVRIP path)")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            self.sock.connect((host, port))
        except OSError as e:
            # DahuaConsole's call site catches PwnlibException to handle
            # connect failures gracefully — wrap so the catch fires.
            raise PwnlibException(f'remote(): connect failed: {e}') from e
        self._connected = True

    def send(self, data):
        if isinstance(data, str):
            data = data.encode('latin-1')
        self.sock.sendall(data)

    def recv(self, numb=4096, timeout=None, **kwargs):
        if timeout is not None:
            old = self.sock.gettimeout()
            self.sock.settimeout(timeout)
            try:
                return self.sock.recv(numb)
            except socket.timeout:
                return b''
            finally:
                self.sock.settimeout(old)
        try:
            return self.sock.recv(numb)
        except socket.timeout:
            return b''

    def recv_raw(self, numb):
        return self.recv(numb)

    def can_recv(self, timeout=0.0):
        try:
            r, _, _ = _select.select([self.sock], [], [], timeout)
            return bool(r)
        except (ValueError, OSError):
            return False

    def connected(self, *args, **kwargs):
        return self._connected

    def close(self):
        if self._connected:
            try:
                self.sock.close()
            except Exception:
                pass
            self._connected = False

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)


def _fmt(size: int, endian: str) -> str:
    e = '<' if endian == 'little' else '>'
    return e + {1: 'B', 2: 'H', 4: 'I', 8: 'Q'}[size]


def _pack(value: int, size: int, endian: str = 'little') -> bytes:
    mask = (1 << (size * 8)) - 1
    return _struct.pack(_fmt(size, endian), value & mask)


def _unpack(data: bytes, size: int, endian: str = 'little') -> int:
    if len(data) != size:
        # pwntools is lenient — pad with zeros on the right (little) or left (big)
        if endian == 'little':
            data = data + b'\x00' * (size - len(data))
        else:
            data = b'\x00' * (size - len(data)) + data
    return _struct.unpack(_fmt(size, endian), data)[0]


def p8 (v, endian='little'): return _pack(v, 1, endian)
def p16(v, endian='little'): return _pack(v, 2, endian)
def p32(v, endian='little'): return _pack(v, 4, endian)
def p64(v, endian='little'): return _pack(v, 8, endian)

def u8 (b, endian='little'): return _unpack(b, 1, endian)
def u16(b, endian='little'): return _unpack(b, 2, endian)
def u32(b, endian='little'): return _unpack(b, 4, endian)
def u64(b, endian='little'): return _unpack(b, 8, endian)


# DahuaConsole sometimes uses `bytes` accidentally where pwntools would
# accept a `str`. The original pwntools versions are also lenient. We
# don't try to match every quirk — only the pack/unpack surface used by
# DahuaConsole's vendored code (verified by grepping the vendored .py
# files for pwntools API calls; only the eight above appear).

__all__ = [
    'p8', 'p16', 'p32', 'p64',
    'u8', 'u16', 'u32', 'u64',
    'log',
    'PwnlibException',
    'remote',
    # stdlib re-exports — pwntools' from-pwn-import-* brings these in
    'base64', 'binascii', 'codecs', 'collections', 'copy', 'datetime',
    'errno', 'functools', 'hashlib', 'hmac', 'io', 'ipaddress',
    'itertools', 'json', 'math', 'os', 'pickle', 'platform', 'random',
    're', 'shlex', 'shutil', 'signal', 'socket', 'string', 'struct',
    'subprocess', 'sys', 'tempfile', 'threading', 'time', 'traceback',
    'types', 'typing', 'urllib', 'uuid', 'warnings', 'weakref',
]

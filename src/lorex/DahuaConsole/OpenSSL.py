"""Minimal OpenSSL shim — replaces pyOpenSSL for DahuaConsole.

DahuaConsole imports `from OpenSSL import crypto` solely for a
CertManager-export debug path that the plugin never invokes. Pulling in
real pyOpenSSL drags in `cryptography` which drags in `cffi 2.0`, which
conflicts with the host's bundled `_cffi_backend 1.16` and breaks plugin
load.

This shim makes `from OpenSSL import crypto` succeed, but any actual
crypto.load_certificate / crypto.dump_publickey calls raise so the bug
is loud if a future code path tries to use them.

Lives inside DahuaConsole/, found before any pip-installed OpenSSL by
sys.path order set in main.py.
"""


class _CryptoStub:
    FILETYPE_PEM = 1
    FILETYPE_ASN1 = 2

    @staticmethod
    def load_certificate(*args, **kwargs):
        raise NotImplementedError(
            'OpenSSL stub: crypto.load_certificate is not implemented; '
            'this code path should not be reachable in the Scrypted plugin')

    @staticmethod
    def dump_publickey(*args, **kwargs):
        raise NotImplementedError(
            'OpenSSL stub: crypto.dump_publickey is not implemented')


crypto = _CryptoStub()

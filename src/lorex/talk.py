"""Reusable Lorex doorbell LAN-direct talk session.

Usage:
    sess = TalkSession(host=DOORBELL_IP, port=35000, user='admin', password=PW)
    sess.open()                  # login + handshake — blocks ~3s
    sess.push_frame(audio_320b)  # one PCMA-8k 40ms frame, call at 25 Hz
    sess.close()

Recipe per ~/Projects/lorex_captures/NOTES.md (2026-04-26 update):
  1. DVRIP login via DahuaConsole → SessionID
  2. AddObject ControlConnection.Passive on auth socket → ConnectionID
  3. Open second TCP, AckSubChannel(SessionID, ConnectionID) → OK
  4. Talk.General config (EncodeFormat:2 Frequency:8000 ConnectionID:...) on auth socket → OK
  5. Push DVRIP-type-0x1d packets at 25 Hz (40ms cadence)
"""
import socket
import struct
import time
import os
import sys
import argparse
import threading

# Vendor DahuaConsole (same dir layout as bridge.py)
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'DahuaConsole'))
sys.path.insert(0, os.path.join(HERE, 'dahua'))   # Mac dev layout

from dahua import DahuaFunctions  # noqa: E402

PCMA_FRAME_BYTES = 882      # 40 ms @ 22.05 kHz
SAMPLE_RATE = 22050

ALAW_SILENCE = 0xd5         # PCMA encoding of 0


class TalkSession:
    """One LAN-direct talk session against a Lorex/Dahua doorbell.

    Thread-safe push: one open(), one close(), push_frame() can be called from
    a single producer thread.
    """

    def __init__(self, host, port=35000, user='admin', password=None,
                 channel=0, talk_mode=0):
        if not password:
            raise ValueError('TalkSession requires a non-empty password')
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.channel = channel
        self.talk_mode = talk_mode

        self.dh = None             # DahuaFunctions (auth socket)
        self.sid = None            # SessionID
        self.conn_id = None        # ConnectionID from AddObject
        self.talk_sock = None      # Second TCP for talk uplink
        self.frame_idx = 0
        self._closed = False

    # === DVRIP text-protocol helpers ===

    def _build_text_cmd(self, method, params, txid=0):
        lines = [f"TransactionID:{txid}", f"Method:{method}"]
        for k, v in params.items():
            lines.append(f"{k}:{v}")
        body = ("\r\n".join(lines) + "\r\n\r\n").encode()
        hdr = struct.pack('<I I', 0xf4, len(body)) + bytes(8) + struct.pack('<I', self.sid) + bytes(12)
        assert len(hdr) == 32
        return hdr + body

    def _send_recv_on_remote(self, pkt, expect=b"FaultCode", timeout=2.0):
        """Send raw bytes through DahuaConsole's auth socket, drain response."""
        self.dh.remote.send(pkt)
        time.sleep(0.3)
        buf = b""
        try:
            while True:
                ch = self.dh.remote.recv(numb=4096, timeout=timeout)
                if not ch:
                    break
                buf += ch
                if expect and expect in buf:
                    break
        except Exception:
            pass
        return buf[32:].decode(errors='replace') if len(buf) > 32 else ""

    def _send_recv_on_talk(self, pkt, expect=b"FaultCode", timeout=2.0):
        """Send + recv on the talk-uplink TCP socket."""
        self.talk_sock.sendall(pkt)
        self.talk_sock.settimeout(timeout)
        buf = b""
        try:
            while True:
                ch = self.talk_sock.recv(8192)
                if not ch:
                    break
                buf += ch
                if expect and expect in buf:
                    break
        except socket.timeout:
            pass
        return buf[32:].decode(errors='replace') if len(buf) > 32 else ""

    def _make_dargs(self):
        ns = argparse.Namespace()
        ns.rhost = self.host; ns.rport = self.port; ns.proto = 'dvrip'
        ns.auth = f'{self.user}:{self.password}'
        ns.ssl = False; ns.relay = None; ns.logon = 'default'
        ns.events = False; ns.multihost = False
        ns.save = False; ns.test = False; ns.dump = False; ns.dump_argv = None
        ns.discover = None; ns.timeout = 5; ns.debug = False
        ns.calls = False; ns.force = False
        return ns

    # === lifecycle ===

    def open(self):
        # 1) DVRIP login
        self.dh = DahuaFunctions(
            rhost=self.host, rport=self.port, proto='dvrip',
            events=False, ssl=False, relay_host=None,
            timeout=10, udp_server=True,
            dargs=self._make_dargs(),
        )
        if not self.dh.dh_connect(username=self.user, password=self.password, logon='default'):
            raise RuntimeError("DVRIP login failed")
        self.sid = self.dh.SessionID

        # 2) AddObject ControlConnection.Passive
        pkt = self._build_text_cmd("AddObject", {
            "ParameterName": "Dahua.Device.Network.ControlConnection.Passive",
            "ConnectProtocol": "0",
        }, txid=130)
        resp = self._send_recv_on_remote(pkt)
        for line in resp.splitlines():
            if line.startswith("ConnectionID:"):
                self.conn_id = line.split(":", 1)[1].strip()
                break
        if not self.conn_id:
            raise RuntimeError(f"AddObject did not return ConnectionID: {resp[:200]}")

        # 3) Open talk uplink TCP, AckSubChannel
        self.talk_sock = socket.create_connection((self.host, self.port), timeout=5)
        ack_pkt = self._build_text_cmd("GetParameterNames", {
            "ParameterName": "Dahua.Device.Network.ControlConnection.AckSubChannel",
            "SessionID": str(self.sid),
            "ConnectionID": self.conn_id,
            "Encrypt": "0",
        }, txid=0)
        resp = self._send_recv_on_talk(ack_pkt)
        if "FaultCode:OK" not in resp:
            raise RuntimeError(f"AckSubChannel rejected: {resp[:200]}")

        # 4) Talk.General config (THE KEY ARMING CALL)
        # The doorbell is single-talk-slot. If a recent session hasn't fully
        # released its slot, this returns FaultCode:Error / ErrorReason:1.
        # Retry with exponential backoff up to ~10s.
        cfg_pkt = self._build_text_cmd("GetParameterNames", {
            "ParameterName": "Dahua.Device.Network.Talk.General",
            "Channel": str(self.channel),
            "EncodeFormat": "2",
            "Depth": "16",
            "Frequency": str(SAMPLE_RATE),
            "State": "1",
            "ConnectionID": self.conn_id,
            "TalkMode": str(self.talk_mode),
            "Type": "0",
        }, txid=131)
        last_resp = ""
        for attempt, wait_s in enumerate([0, 1.5, 3.0, 5.0]):
            if wait_s:
                time.sleep(wait_s)
            resp = self._send_recv_on_remote(cfg_pkt)
            last_resp = resp
            if "FaultCode:OK" in resp:
                break
        else:
            raise RuntimeError(f"Talk.General rejected after retries: {last_resp[:200]}")

    # === DHAV / DVRIP audio framing ===

    def _pack_dhav_main(self, audio_len):
        t = time.localtime()
        pt = ((t.tm_sec & 0x3f) | ((t.tm_min & 0x3f) << 6) |
              ((t.tm_hour & 0x1f) << 12) | ((t.tm_mday & 0x1f) << 17) |
              ((t.tm_mon & 0xf) << 22) | (((t.tm_year - 2000 + 0x30) & 0x3f) << 26))
        ts = ((self.frame_idx + 1) * 0x14) & 0xffff
        buf = bytearray(28)
        buf[0:4] = b"DHAV"; buf[4] = 0xf0
        buf[8:12] = struct.pack("<I", self.frame_idx)
        buf[12:16] = struct.pack("<I", audio_len + 0x24)
        buf[16:20] = struct.pack("<I", pt)
        buf[20:22] = struct.pack("<H", ts)
        buf[22] = 0x04
        buf[23] = sum(buf[0:23]) & 0xff
        buf[24:28] = bytes([0x83, 0x01, 0x0e, 0x06])  # PCMA, 22.05 kHz (sample-rate code 0x06)
        return bytes(buf)

    def push_frame(self, audio_bytes):
        """Push one 320-byte PCMA-8k frame. Pad with silence if short."""
        if self._closed:
            raise RuntimeError("session closed")
        if len(audio_bytes) < PCMA_FRAME_BYTES:
            audio_bytes = audio_bytes + bytes([ALAW_SILENCE]) * (PCMA_FRAME_BYTES - len(audio_bytes))
        elif len(audio_bytes) > PCMA_FRAME_BYTES:
            audio_bytes = audio_bytes[:PCMA_FRAME_BYTES]

        main = self._pack_dhav_main(len(audio_bytes))
        trailer = b"dhav" + struct.pack("<I", len(audio_bytes) + 0x24)
        dhav = main + audio_bytes + trailer
        # 32-byte DVRIP audio header (verbatim from Lorex Pro pcap)
        hdr = bytearray(32)
        hdr[0] = 0x1d
        hdr[4:8] = struct.pack("<I", len(dhav))
        hdr[8:12] = bytes([0x02, 0x10, 0x00, 0x00])
        hdr[12:16] = bytes([0x00, 0x02, 0x00, 0x00])
        hdr[16:20] = bytes([0x00, 0x40, 0x1f, 0x00])
        self.talk_sock.sendall(bytes(hdr) + dhav)
        self.frame_idx += 1

    def close(self):
        if self._closed:
            return
        self._closed = True
        # 1) Tell doorbell to free the talk slot. Without this, the
        # ControlConnection.Passive object lingers server-side and the next
        # Talk.General call gets FaultCode:Error / ErrorReason:1.
        if self.dh and self.conn_id:
            try:
                rm_pkt = self._build_text_cmd("RemoveObject", {
                    "ParameterName": "Dahua.Device.Network.ControlConnection.Passive",
                    "ConnectionID": self.conn_id,
                }, txid=132)
                self._send_recv_on_remote(rm_pkt, timeout=1.0)
            except Exception:
                pass
        # 2) Close talk uplink TCP
        try:
            if self.talk_sock:
                self.talk_sock.close()
        except Exception:
            pass
        # 3) Logout / close auth socket
        try:
            if self.dh:
                self.dh.cleanup()
        except Exception:
            pass


def stream_stdin_to_doorbell(host, port=35000,
                             user='admin', password=None,
                             stdin=None):
    """CLI mode: read raw PCMA-8k mono bytes from stdin in 320-byte chunks
    and push to doorbell at 25 Hz cadence (paced by stdin arrival rate).

    Caller is expected to feed ~40 ms / 320 B chunks at real-time rate
    (e.g. ffmpeg piping at 8 kHz mono A-law)."""
    if stdin is None:
        stdin = sys.stdin.buffer

    sess = TalkSession(host=host, port=port, user=user, password=password)
    sess.open()
    sys.stderr.write(f"[talk] session open, sid={sess.sid} conn_id={sess.conn_id}\n")
    sys.stderr.flush()

    sent = 0
    start = time.monotonic()
    try:
        while True:
            chunk = stdin.read(PCMA_FRAME_BYTES)
            if not chunk:
                break
            sess.push_frame(chunk)
            sent += 1
            # Pace at 25 Hz (40ms per frame). If upstream is real-time-paced
            # (e.g. ffmpeg -re), this loop will mostly no-op; otherwise it
            # protects the doorbell from a flood that would drop frames.
            target = start + sent * 0.04
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
    finally:
        elapsed = time.monotonic() - start
        sys.stderr.write(f"[talk] sent {sent} frames in {elapsed:.2f}s ({sent/max(elapsed,0.001):.1f}fps)\n")
        sys.stderr.flush()
        sess.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--host', required=True, help='Doorbell IP address')
    p.add_argument('--port', type=int, default=35000)
    p.add_argument('--user', default='admin')
    p.add_argument('--password', required=True, help='Doorbell password')
    args = p.parse_args()
    stream_stdin_to_doorbell(args.host, args.port, args.user, args.password)

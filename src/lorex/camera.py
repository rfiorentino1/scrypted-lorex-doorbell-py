"""LorexDoorbellCamera — the actual HomeKit-facing device.

Implements Camera/VideoCamera/BinarySensor/MotionSensor/Settings/Intercom.
DVRIP connection lifecycle is owned here (replacing the old separate
bridge.py daemon + SSE plumbing). Press-event semantics match the prior
TS plugin: triggerPress is idempotent within a 3s hold window so a real
press chain (BackKeyLight + VideoTalk + CallNoAnswered + PhoneCallDetect
all firing within ~20ms) only produces ONE HomeKit notification.
"""
import asyncio
import datetime
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Set

import scrypted_sdk
from scrypted_sdk import (
    MediaObject,
    ScryptedDeviceBase,
    ScryptedInterface,
    Setting,
    SettingValue,
)

from lorex.dvrip_client import DvripClient
from lorex.snapshot import take_snapshot_via_ffmpeg
from lorex.talk import TalkSession

log = logging.getLogger('lorex.camera')


# Press semantics: any of these codes (Start or Pulse) means the button
# was pressed. We OR-trigger because Lorex Skywatch firmware emits a
# different subset on each press; relying on any single code misses
# presses. Each code below has been verified against the bridge log to
# fire ONLY in press chains, never during motion-only events.
#
# DO NOT add `BackKeyLight` here. It was previously included on the
# (mistaken) assumption that the button-LED pulse was press-only, but
# the bridge log shows BackKeyLight Pulse firing during motion-only
# sequences (e.g. 2026-04-24 19:06:21 and 21:59:03/30 — VideoMotion +
# CrossRegionDetection chains with no VideoTalk/CallNoAnswered/
# PhoneCallDetect). The doorbell pulses the button LED for visibility
# during motion at night, so it's NOT a reliable press signal. Real
# presses still fire VideoTalk + CallNoAnswered + PhoneCallDetect
# within ~20ms of each other, which is enough to catch every press.
PRESS_CODES: Set[str] = {
    'VideoTalk',
    'PhoneCallDetect',
    'CallNoAnswered',
    'VideoTalkInvite',
}

MOTION_CODES: Set[str] = {
    'VideoMotion',
    'SmartMotionHuman',
    'CrossLineDetection',
    'CrossRegionDetection',
}

PRESS_HOLD_SEC = 3.0
MOTION_HOLD_SEC = 5.0


class LorexDoorbellCamera(ScryptedDeviceBase):
    def __init__(self, nativeId: str, provider: Any):
        super().__init__(nativeId)
        self._provider = provider
        self._loop = asyncio.get_event_loop()

        self._dvrip: Optional[DvripClient] = None
        self._press_reset: Optional[asyncio.TimerHandle] = None
        self._motion_reset: Optional[asyncio.TimerHandle] = None
        self._seen_codes: Set[str] = set()

        self._talk: Optional[TalkSession] = None
        self._intercom_task: Optional[asyncio.Task] = None
        self._intercom_proc: Optional[asyncio.subprocess.Process] = None

        # Initial state
        self.binaryState = False
        self.motionDetected = False

        # Start the DVRIP connection if we have credentials persisted.
        self.restart_connection()

    # ===== DVRIP connection lifecycle =====

    def _credentials_ready(self) -> bool:
        return bool(self.storage.getItem('ip')
                    and self.storage.getItem('password'))

    def restart_connection(self) -> None:
        """Tear down any existing DVRIP connection and start a new one
        with the current settings. Safe to call repeatedly (e.g. after
        putSetting changes credentials)."""
        # Stop existing
        if self._dvrip is not None:
            try:
                self._dvrip.stop()
            except Exception as e:
                log.warning('dvrip stop raised: %r', e)
            self._dvrip = None

        if not self._credentials_ready():
            log.info('[%s] credentials not yet configured; DVRIP idle',
                     self.nativeId)
            return

        ip = self.storage.getItem('ip')
        port = int(self.storage.getItem('dvripPort') or 35000)
        user = self.storage.getItem('username') or 'admin'
        pwd = self.storage.getItem('password') or ''

        log.info('[%s] starting DVRIP connection to %s:%d',
                 self.nativeId, ip, port)
        self._dvrip = DvripClient(
            host=ip, port=port, user=user, password=pwd,
            on_event=self._on_dvrip_event_threadsafe,
            on_state=self._on_dvrip_state_threadsafe,
        )
        self._dvrip.start()

    # Called from the DVRIP background thread — must marshal back to the
    # asyncio event loop before touching device state.
    def _on_dvrip_event_threadsafe(self, evt: Dict[str, Any]) -> None:
        try:
            self._loop.call_soon_threadsafe(self._handle_event, evt)
        except RuntimeError:
            # Loop closed during shutdown
            pass

    def _on_dvrip_state_threadsafe(self, connected: bool) -> None:
        try:
            self._loop.call_soon_threadsafe(
                lambda: log.info('[%s] DVRIP %s', self.nativeId,
                                 'connected' if connected else 'disconnected'))
        except RuntimeError:
            pass

    # ===== Event handling (runs on the asyncio loop) =====

    def _handle_event(self, evt: Dict[str, Any]) -> None:
        code = evt.get('code') or ''
        action = evt.get('action') or ''

        if code and code not in self._seen_codes:
            self._seen_codes.add(code)
            log.info('[%s] First-time event: %s action=%s',
                     self.nativeId, code, action)

        if code in PRESS_CODES:
            if action in ('Start', 'Pulse'):
                self._trigger_press()
            elif action == 'Stop':
                # Real Stop fires much later than the press hold; ignore
                # to avoid prematurely clearing the button-press state.
                pass
            return

        if code in MOTION_CODES:
            if action in ('Start', 'Pulse'):
                self._trigger_motion()
            elif action == 'Stop':
                # Same reasoning — the auto-reset timer governs the
                # visible state.
                pass
            return

    def _trigger_press(self) -> None:
        # Idempotent within hold window: only flip false→true on the
        # FIRST event in a press chain. Subsequent events refresh the
        # auto-reset timer but don't re-emit binaryState.
        first_press_in_chain = not self.binaryState
        if first_press_in_chain:
            self.binaryState = True
        if self._press_reset is not None:
            self._press_reset.cancel()
        self._press_reset = self._loop.call_later(
            PRESS_HOLD_SEC, self._reset_press)

        # Optional webhook for external integrations (e.g. NAS-side event
        # extractor that builds pre-roll+post-roll clips from continuous
        # recordings). Fire only on the first press in a chain so the
        # external system gets one notification per real press, mirroring
        # HomeKit's binaryState semantics.
        if first_press_in_chain:
            webhook_url = (self.storage.getItem('pressWebhookUrl') or '').strip()
            if webhook_url:
                press_ts = datetime.datetime.now().isoformat(timespec='seconds')
                threading.Thread(
                    target=self._fire_press_webhook,
                    args=(webhook_url, press_ts),
                    daemon=True,
                ).start()

    def _fire_press_webhook(self, url: str, timestamp: str) -> None:
        """Best-effort POST. Never raises — webhook failures must not
        affect the press handler or HomeKit notification path."""
        try:
            payload = json.dumps({
                'timestamp': timestamp,
                'deviceId': self.nativeId,
                'deviceName': self.providedName or self.nativeId,
                'event': 'press',
            }).encode('utf-8')
            req = urllib.request.Request(
                url,
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            urllib.request.urlopen(req, timeout=3).read()
        except Exception as e:  # noqa: BLE001 - intentionally swallowed
            log.warning('[%s] press webhook to %s failed: %s',
                        self.nativeId, url, e)

    def _reset_press(self) -> None:
        self.binaryState = False
        self._press_reset = None

    def _trigger_motion(self) -> None:
        if not self.motionDetected:
            self.motionDetected = True
        if self._motion_reset is not None:
            self._motion_reset.cancel()
        self._motion_reset = self._loop.call_later(
            MOTION_HOLD_SEC, self._reset_motion)

    def _reset_motion(self) -> None:
        self.motionDetected = False
        self._motion_reset = None

    # ===== Settings =====

    async def getSettings(self) -> List[Setting]:
        return [
            {
                'key': 'ip',
                'title': 'Doorbell IP',
                'value': self.storage.getItem('ip') or '',
                'description': 'LAN IP of the doorbell. Used for both DVRIP and the derived RTSP URL.',
            },
            {
                'key': 'username',
                'title': 'Username',
                'value': self.storage.getItem('username') or 'admin',
            },
            {
                'key': 'password',
                'title': 'Password',
                'type': 'password',
                'value': self.storage.getItem('password') or '',
            },
            {
                'key': 'dvripPort',
                'title': 'DVRIP Port',
                'type': 'number',
                'value': int(self.storage.getItem('dvripPort') or 35000),
                'description': '35000 for Lorex/Skywatch firmware, 37777 for stock Dahua.',
                'subgroup': 'Advanced',
            },
            {
                'key': 'rtspUrl',
                'title': 'RTSP URL Override',
                'value': self.storage.getItem('rtspUrl') or '',
                'description': 'Optional. If blank, derived from IP + credentials.',
                'subgroup': 'Advanced',
            },
            {
                'key': 'snapshotUrl',
                'title': 'Snapshot URL Override',
                'value': self.storage.getItem('snapshotUrl') or '',
                'description': 'Optional direct JPEG snapshot URL (most Skywatch firmware blocks port 80, leave blank to use ffmpeg from RTSP).',
                'subgroup': 'Advanced',
            },
            {
                'key': 'pressWebhookUrl',
                'title': 'Press Webhook URL',
                'value': self.storage.getItem('pressWebhookUrl') or '',
                'description': 'Optional. POSTs JSON {timestamp, deviceId, deviceName, event:"press"} '
                               'to this URL on each press (first event in a chain only — mirrors '
                               'HomeKit notification semantics). Useful for external recording / '
                               'event-clip pipelines. Failures are logged and ignored; HomeKit '
                               'notifications are unaffected.',
                'subgroup': 'Advanced',
            },
            # Debug actions (replace the old /inject HTTP endpoint).
            {
                'key': 'testPress',
                'title': 'Trigger Test Press',
                'type': 'button',
                'description': 'Synthetically fire a doorbell press event to verify HomeKit notifications without bothering anyone.',
                'subgroup': 'Debug',
            },
            {
                'key': 'testMotion',
                'title': 'Trigger Test Motion',
                'type': 'button',
                'subgroup': 'Debug',
            },
        ]

    async def putSetting(self, key: str, value: SettingValue) -> None:
        if key == 'testPress':
            log.info('[%s] DEBUG: synthetic press triggered from Settings',
                     self.nativeId)
            self._trigger_press()
            return
        if key == 'testMotion':
            log.info('[%s] DEBUG: synthetic motion triggered from Settings',
                     self.nativeId)
            self._trigger_motion()
            return

        self.storage.setItem(key, '' if value is None else str(value))
        await scrypted_sdk.deviceManager.onDeviceEvent(
            self.nativeId, ScryptedInterface.Settings.value, None)

        # Restart DVRIP if the connection params changed.
        if key in ('ip', 'username', 'password', 'dvripPort'):
            self.restart_connection()

    # ===== Camera / VideoCamera / Snapshot =====

    def _rtsp_url(self) -> str:
        override = self.storage.getItem('rtspUrl')
        if override:
            return override
        ip = self.storage.getItem('ip') or ''
        user = self.storage.getItem('username') or 'admin'
        pwd = self.storage.getItem('password') or ''
        # Lorex/Dahua RTSP path with main stream
        return f'rtsp://{user}:{pwd}@{ip}:554/cam/realmonitor?channel=1&subtype=0'

    async def takePicture(self, options: Any = None) -> MediaObject:
        log.info('[%s] takePicture called', self.nativeId)
        snap_url = self.storage.getItem('snapshotUrl')
        if snap_url:
            log.info('[%s] takePicture: using snapshotUrl override', self.nativeId)
            return await scrypted_sdk.mediaManager.createMediaObjectFromUrl(snap_url)

        rtsp = self._rtsp_url()
        if not rtsp.startswith('rtsp://'):
            raise Exception('No RTSP URL configured')
        ffmpeg_path = await scrypted_sdk.mediaManager.getFFmpegPath()
        log.info('[%s] takePicture: spawning ffmpeg', self.nativeId)
        jpeg = await take_snapshot_via_ffmpeg(ffmpeg_path, rtsp)
        log.info('[%s] takePicture: got %d-byte JPEG', self.nativeId, len(jpeg))
        return await scrypted_sdk.mediaManager.createMediaObject(
            jpeg, 'image/jpeg')

    async def getPictureOptions(self) -> List[Any]:
        return []

    async def getVideoStream(self, options: Any = None) -> MediaObject:
        log.info('[%s] getVideoStream called', self.nativeId)
        rtsp = self._rtsp_url()
        return await scrypted_sdk.mediaManager.createFFmpegMediaObject({
            'url': rtsp,
            'container': 'rtsp',
            'inputArguments': ['-rtsp_transport', 'tcp', '-i', rtsp],
            'mediaStreamOptions': {
                'id': 'main',
                'name': 'Main Stream',
                'container': 'rtsp',
                'video': {'codec': 'h264'},
                'audio': {'codec': 'pcm_alaw'},
                'tool': 'scrypted',
            },
        })

    async def getVideoStreamOptions(self) -> List[Any]:
        return [{
            'id': 'main',
            'name': 'Main Stream',
            'container': 'rtsp',
            'video': {'codec': 'h264'},
            'audio': {'codec': 'pcm_alaw'},
            'tool': 'scrypted',
        }]

    # ===== Intercom (two-way audio via DVRIP-pushed PCMA frames) =====

    async def startIntercom(self, media: MediaObject) -> None:
        log.info('[%s] startIntercom called', self.nativeId)
        if self._talk is not None:
            log.info('[%s] startIntercom: prior session still active, stopping it first',
                     self.nativeId)
            await self.stopIntercom()

        ip = self.storage.getItem('ip') or ''
        port = int(self.storage.getItem('dvripPort') or 35000)
        user = self.storage.getItem('username') or 'admin'
        pwd = self.storage.getItem('password') or ''
        if not ip or not pwd:
            raise Exception('Intercom requires IP and password to be configured')

        # Get ffmpeg input args from the inbound HomeKit media object FIRST
        # (before opening the talk session) so a converter-not-found error
        # doesn't leave us with a leaked TalkSession.
        ffmpeg_input = await scrypted_sdk.mediaManager.convertMediaObjectToJSON(
            media, 'x-scrypted/x-ffmpeg-input')
        ffmpeg_path = await scrypted_sdk.mediaManager.getFFmpegPath()

        # Now open the talk session — login + handshake blocks ~3s.
        self._talk = TalkSession(host=ip, port=port, user=user, password=pwd)
        try:
            await asyncio.to_thread(self._talk.open)
            log.info('[%s] intercom: talk session open (sid=%s conn_id=%s)',
                     self.nativeId, self._talk.sid, self._talk.conn_id)

            # Spawn ffmpeg to transcode whatever HomeKit gives us into
            # PCMA at 22.05 kHz mono — the rate Lorex/Skywatch expects on
            # its DVRIP talk channel (882 bytes per 40ms frame). The
            # talk.py docstring labels it "PCMA-8k" but the wire format
            # is actually 22.05k; sending true 8 kHz produces silence
            # because each 320-byte 40ms frame gets padded to 882 with
            # a-law silence inside push_frame.
            args = list(ffmpeg_input.get('inputArguments') or [])
            args.extend([
                '-vn', '-acodec', 'pcm_alaw', '-ar', '22050', '-ac', '1',
                '-f', 'alaw', 'pipe:1',
            ])
            log.info('[%s] intercom: ffmpeg args: %s', self.nativeId, ' '.join(args))
            self._intercom_proc = await asyncio.create_subprocess_exec(
                ffmpeg_path, *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            log.info('[%s] intercom: ffmpeg spawned pid=%s, starting pump',
                     self.nativeId, self._intercom_proc.pid)
            self._intercom_task = asyncio.create_task(self._intercom_pump())
        except Exception as e:
            # Failure path: clean up partially-initialized talk session
            # so the next attempt isn't blocked by stale state.
            log.error('[%s] intercom: startIntercom failed: %r', self.nativeId, e)
            try:
                await asyncio.to_thread(self._talk.close)
            except Exception:
                pass
            self._talk = None
            raise

    async def _intercom_pump(self) -> None:
        FRAME = 882  # PCMA @ 22.05 kHz mono, 40ms — matches talk.PCMA_FRAME_BYTES
        proc = self._intercom_proc
        if proc is None or proc.stdout is None:
            log.error('[%s] intercom pump: no proc/stdout', self.nativeId)
            return
        frames_sent = 0
        try:
            buf = b''
            while True:
                chunk = await proc.stdout.read(8192)
                if not chunk:
                    log.info('[%s] intercom pump: ffmpeg stdout EOF after %d frames',
                             self.nativeId, frames_sent)
                    break
                buf += chunk
                while len(buf) >= FRAME:
                    frame, buf = buf[:FRAME], buf[FRAME:]
                    if self._talk is not None:
                        await asyncio.to_thread(self._talk.push_frame, frame)
                        frames_sent += 1
                        if frames_sent in (1, 25, 250):
                            log.info('[%s] intercom pump: pushed %d frames',
                                     self.nativeId, frames_sent)
            if buf and self._talk is not None:
                await asyncio.to_thread(self._talk.push_frame, buf)
        except asyncio.CancelledError:
            log.info('[%s] intercom pump: cancelled (%d frames pushed)',
                     self.nativeId, frames_sent)
            raise
        except Exception as e:
            log.error('[%s] intercom pump error after %d frames: %r',
                      self.nativeId, frames_sent, e)
            # Surface ffmpeg stderr for diagnosis
            if proc is not None and proc.stderr is not None:
                try:
                    err = await proc.stderr.read(2000)
                    if err:
                        log.error('[%s] intercom ffmpeg stderr: %s',
                                  self.nativeId, err.decode('utf-8', 'replace')[:1000])
                except Exception:
                    pass

    async def stopIntercom(self) -> None:
        if self._intercom_task is not None:
            self._intercom_task.cancel()
            try:
                await self._intercom_task
            except (asyncio.CancelledError, Exception):
                pass
            self._intercom_task = None

        if self._intercom_proc is not None:
            try:
                self._intercom_proc.kill()
                await self._intercom_proc.wait()
            except Exception:
                pass
            self._intercom_proc = None

        if self._talk is not None:
            try:
                await asyncio.to_thread(self._talk.close)
            except Exception as e:
                log.warning('[%s] talk close raised: %r', self.nativeId, e)
            self._talk = None

    # ===== Lifecycle =====

    async def shutdown(self) -> None:
        await self.stopIntercom()
        if self._press_reset is not None:
            self._press_reset.cancel()
        if self._motion_reset is not None:
            self._motion_reset.cancel()
        if self._dvrip is not None:
            self._dvrip.stop()
            self._dvrip = None

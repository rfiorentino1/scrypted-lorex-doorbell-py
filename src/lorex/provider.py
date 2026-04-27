"""DeviceProvider + DeviceCreator for the Lorex doorbell plugin.

Owns one or more LorexDoorbellCamera instances, one per configured
doorbell. Each is created via the Scrypted UI's "Add Device" flow,
which calls createDevice() with name + address fields.
"""
import asyncio
import logging
from typing import Any, Dict, List

import scrypted_sdk
from scrypted_sdk import (
    ScryptedDeviceBase,
    ScryptedInterface,
    ScryptedDeviceType,
    Setting,
    SettingValue,
)

from lorex.camera import LorexDoorbellCamera

log = logging.getLogger('lorex.provider')


class LorexDoorbellProvider(ScryptedDeviceBase):
    def __init__(self, nativeId: str = None):
        super().__init__(nativeId)
        self._cameras: Dict[str, LorexDoorbellCamera] = {}
        asyncio.ensure_future(self._initialize())

    async def _initialize(self) -> None:
        """Re-announce previously-created devices on startup so Scrypted
        re-binds them. The actual LorexDoorbellCamera instances are
        constructed lazily in getDevice()."""
        try:
            existing = scrypted_sdk.deviceManager.getNativeIds() or []
        except Exception:
            existing = []
        # Filter out None (the provider's own nativeId)
        existing = [n for n in existing if n]
        if not existing:
            log.info('no previously-configured doorbells')
            return
        log.info('restoring %d previously-configured doorbell(s): %s',
                 len(existing), existing)
        # Touch each one so getDevice() runs and connections start.
        for native_id in existing:
            try:
                await self.getDevice(native_id)
            except Exception as e:
                log.error('failed to restore %s: %r', native_id, e)

    # ----- DeviceProvider -----

    async def getDevice(self, nativeId: str) -> Any:
        if nativeId not in self._cameras:
            self._cameras[nativeId] = LorexDoorbellCamera(nativeId, self)
        return self._cameras[nativeId]

    async def releaseDevice(self, id: str, nativeId: str) -> None:
        cam = self._cameras.pop(nativeId, None)
        if cam is not None:
            try:
                await cam.shutdown()
            except Exception as e:
                log.warning('shutdown of %s raised: %r', nativeId, e)

    # ----- DeviceCreator -----

    async def getCreateDeviceSettings(self) -> List[Setting]:
        return [
            {
                'key': 'name',
                'title': 'Name',
                'placeholder': 'Front Door',
                'description': 'Display name in HomeKit / Scrypted.',
            },
            {
                'key': 'ip',
                'title': 'Doorbell IP',
                'placeholder': '192.168.1.100',
                'description': 'LAN IP of the doorbell.',
            },
            {
                'key': 'username',
                'title': 'Username',
                'value': 'admin',
            },
            {
                'key': 'password',
                'title': 'Password',
                'type': 'password',
            },
            {
                'key': 'dvripPort',
                'title': 'DVRIP Port',
                'type': 'number',
                'value': 35000,
                'description': 'Default 35000 for Lorex/Skywatch firmware. Use 37777 for stock Dahua.',
            },
        ]

    async def createDevice(self, settings: Dict[str, SettingValue]) -> str:
        name = (settings.get('name') or 'Lorex Doorbell').strip()
        ip = (settings.get('ip') or '').strip()
        if not ip:
            raise Exception('Doorbell IP is required')

        native_id = f'lorex-{ip.replace(".", "-")}'

        # Persist initial settings. The camera's storage is namespaced by
        # nativeId — we write through deviceManager so they survive even
        # before the camera object exists.
        await scrypted_sdk.deviceManager.onDeviceDiscovered({
            'nativeId': native_id,
            'name': name,
            'type': ScryptedDeviceType.Doorbell.value,
            'interfaces': [
                ScryptedInterface.Camera.value,
                ScryptedInterface.VideoCamera.value,
                ScryptedInterface.BinarySensor.value,
                ScryptedInterface.MotionSensor.value,
                ScryptedInterface.Settings.value,
                ScryptedInterface.Intercom.value,
            ],
        })

        # Pre-populate storage with the values from the create flow.
        cam = await self.getDevice(native_id)
        for key in ('ip', 'username', 'password', 'dvripPort'):
            v = settings.get(key)
            if v is not None and v != '':
                cam.storage.setItem(key, str(v))
        # Kick the camera into starting its DVRIP connection now that
        # credentials are persisted.
        cam.restart_connection()
        return native_id

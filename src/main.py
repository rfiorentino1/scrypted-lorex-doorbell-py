"""Scrypted plugin entry point for the Lorex doorbell.

Single Python plugin: connects directly to the doorbell over DVRIP, exposes
HomeKit BinarySensor (button), MotionSensor, Camera/VideoCamera (RTSP),
Snapshot, and Intercom (two-way audio via DVRIP-pushed PCMA frames).

No SSE bridge daemon, no HTTP server, no systemd unit. Settings live in the
Scrypted UI; credentials are entered once when adding the device.
"""
import asyncio
import logging
import os
import sys

# Make the vendored DahuaConsole importable. dahua.py imports its sibling
# modules with `from net import ...` (no package prefix), so the directory
# must be on sys.path.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'lorex', 'DahuaConsole'))

import scrypted_sdk

from lorex.provider import LorexDoorbellProvider
from lorex.logging_bridge import install_logging_bridge


def create_scrypted_plugin():
    # Route stdlib `logging` and DahuaConsole/pwntools chatter into the
    # Scrypted per-plugin console. Without this, log.info/etc are silent.
    install_logging_bridge()
    return LorexDoorbellProvider()

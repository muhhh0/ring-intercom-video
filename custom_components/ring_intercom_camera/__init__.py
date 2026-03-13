"""Ring Intercom Video Camera integration.

Adds a WebRTC live-stream camera entity for Ring Intercom Video
(intercom_handset_video) devices. The official Ring integration only creates
lock/ding entities for intercoms; this component adds the missing camera.

Architecture:
- Hooks into the existing Ring integration's data/auth
- Monkey-patches RingOther to add WebRTC stream methods (same as RingDoorBell)
- Exposes a native HA WebRTC camera entity (browser does the WebRTC, no aiortc needed)
- When user opens the camera in Lovelace, the browser establishes WebRTC directly
"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv, discovery

_LOGGER = logging.getLogger(__name__)

DOMAIN = "ring_intercom_camera"
PLATFORMS = [Platform.CAMERA]

# Custom service: ring_intercom_camera.record
SERVICE_RECORD = "record"
SERVICE_RECORD_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required("filename"): cv.string,
        vol.Required("duration"): vol.All(vol.Coerce(int), vol.Range(min=1, max=300)),
        vol.Optional("lookback", default=0): vol.Coerce(int),
    }
)


def _patch_ring_other() -> None:
    """Add WebRTC stream methods to RingOther (intercom) class.

    RingOther doesn't inherit from RingDoorBell so it lacks WebRTC methods,
    even though the intercom_handset_video hardware supports WebRTC live view
    via the exact same signaling protocol.
    """
    from ring_doorbell.other import RingOther
    from ring_doorbell.webrtcstream import RingWebRtcStream

    if hasattr(RingOther, "generate_async_webrtc_stream"):
        return  # Already patched

    def _get_streams(self):
        """Lazy-init _webrtc_streams for already-instantiated objects."""
        if not hasattr(self, "_webrtc_streams"):
            self._webrtc_streams = {}
        return self._webrtc_streams

    async def generate_async_webrtc_stream(self, sdp_offer, session_id,
                                            on_message_callback, *,
                                            keep_alive_timeout=60 * 5):
        streams = _get_streams(self)

        async def _close_callback():
            await self.close_webrtc_stream(session_id)

        stream = RingWebRtcStream(
            self._ring,
            self.device_api_id,
            on_message_callback=on_message_callback,
            keep_alive_timeout=keep_alive_timeout,
            on_close_callback=_close_callback,
        )
        streams[session_id] = stream
        await stream.generate(sdp_offer)

    async def on_webrtc_candidate(self, session_id, candidate, multi_line_index):
        streams = _get_streams(self)
        if stream := streams.get(session_id):
            await stream.on_ice_candidate(candidate, multi_line_index)

    async def close_webrtc_stream(self, session_id):
        streams = _get_streams(self)
        stream = streams.pop(session_id, None)
        if stream:
            await stream.close()

    def sync_close_webrtc_stream(self, session_id):
        streams = _get_streams(self)
        stream = streams.pop(session_id, None)
        if stream:
            stream.sync_close()
    RingOther.generate_async_webrtc_stream = generate_async_webrtc_stream
    RingOther.on_webrtc_candidate = on_webrtc_candidate
    RingOther.close_webrtc_stream = close_webrtc_stream
    RingOther.sync_close_webrtc_stream = sync_close_webrtc_stream

    _LOGGER.info("Patched RingOther with WebRTC stream methods")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Ring Intercom Camera component from configuration.yaml."""
    hass.data.setdefault(DOMAIN, {})

    _patch_ring_other()

    hass.async_create_task(
        discovery.async_load_platform(hass, Platform.CAMERA, DOMAIN, {}, config)
    )

    async def handle_record_service(call: ServiceCall) -> None:
        """Handle ring_intercom_camera.record service call."""
        entity_id = call.data[ATTR_ENTITY_ID]
        filename = call.data["filename"]
        duration = call.data["duration"]
        lookback = call.data.get("lookback", 0)

        # Find the camera entity via the camera component's entity registry
        component = hass.data.get("camera")
        if not component:
            _LOGGER.error("Camera component not loaded")
            return

        entity = component.get_entity(entity_id)
        if not entity:
            _LOGGER.error("Camera entity %s not found", entity_id)
            return

        _LOGGER.info(
            "Recording service called for %s: %s (%ds)",
            entity_id, filename, duration,
        )
        await entity.async_record(filename, duration, lookback)

    hass.services.async_register(
        DOMAIN, SERVICE_RECORD, handle_record_service, schema=SERVICE_RECORD_SCHEMA
    )

    return True

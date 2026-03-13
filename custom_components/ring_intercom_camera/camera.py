"""Camera platform for Ring Intercom Video.

Three modes of operation:
1. LIVE STREAM (browser WebRTC) — user opens camera card in Lovelace,
   browser establishes WebRTC peer connection directly to Ring.
   This entity acts as signaling bridge only.

2. SNAPSHOT (server-side WebRTC) — camera.snapshot service or
   async_camera_image() triggers a server-side WebRTC connection
   using aiortc, captures a stabilized video frame, returns JPEG.
   Works from automations without needing a browser open.

3. RECORDING (server-side WebRTC) — camera.record service or
   async_record() establishes a server-side WebRTC connection using
   aiortc, receives video+audio frames, and writes them as an MP4 file
   using PyAV. Recording runs for a specified duration then stops.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from io import BytesIO
from typing import Any

from ring_doorbell.webrtcstream import RingWebRtcMessage

from homeassistant.components.camera import (
    Camera,
    CameraEntityFeature,
    RTCIceCandidateInit,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCError,
    WebRTCSendMessage,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

# Server-side snapshot capture settings
SNAPSHOT_MAX_FRAMES = 75         # Max frames to examine (~3s at 25fps)
SNAPSHOT_BRIGHTNESS_THRESHOLD = 25  # Min brightness to consider "real" video
SNAPSHOT_STABILIZE_FRAMES = 5    # Consecutive bright frames before capture
SNAPSHOT_CACHE_SECONDS = 10      # Don't re-capture within this window


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up Ring Intercom camera entities."""
    ring_entries = hass.config_entries.async_entries("ring")
    if not ring_entries:
        _LOGGER.warning("Ring integration not configured")
        return

    entities = []
    for entry in ring_entries:
        ring_data = getattr(entry, "runtime_data", None)
        if ring_data is None:
            continue

        try:
            devices = ring_data.devices
            for device in devices.other:
                if device.kind == "intercom_handset_video":
                    _LOGGER.info(
                        "Found Ring Intercom Video: %s (id: %s)",
                        device.name, device.device_api_id,
                    )
                    entities.append(RingIntercomCamera(device))
        except Exception:
            _LOGGER.exception("Error discovering Ring Intercom Video devices")

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Added %d Ring Intercom Video camera(s)", len(entities))
    else:
        _LOGGER.info("No Ring Intercom Video devices found")


class RingIntercomCamera(Camera):
    """WebRTC live-stream camera + server-side snapshot for Ring Intercom Video."""

    def __init__(self, device) -> None:
        """Initialize the camera."""
        super().__init__()
        self._device = device
        self._attr_name = f"{device.name} Camera"
        self._attr_unique_id = f"ring_intercom_camera_{device.device_api_id}"
        self._attr_brand = "Ring"
        self._attr_model = "Intercom Video"
        self._attr_supported_features = CameraEntityFeature.STREAM

        # Snapshot cache
        self._last_image: bytes | None = None
        self._last_image_time: float = 0
        self._capturing: bool = False

        # Recording state
        self._is_recording: bool = False
        self._recording_file: str | None = None

    @property
    def is_recording(self) -> bool:
        return self._is_recording

    @property
    def motion_detection_enabled(self) -> bool:
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {
            "device_id": self._device.device_api_id,
            "device_kind": self._device.kind,
            "stream_method": "webrtc_native",
            "last_snapshot": self._last_image_time or None,
        }
        if self._recording_file:
            attrs["recording_file"] = self._recording_file
        return attrs

    # ---- Snapshot (server-side WebRTC capture) ----

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Capture a snapshot via server-side WebRTC.

        Returns cached image if recent, otherwise starts a new
        WebRTC session with aiortc to grab a stabilized frame.
        """
        # Return cache if fresh
        if (
            self._last_image
            and (time.time() - self._last_image_time) < SNAPSHOT_CACHE_SECONDS
        ):
            return self._last_image

        # Avoid concurrent captures
        if self._capturing:
            return self._last_image

        self._capturing = True
        try:
            image = await self._capture_snapshot()
            if image and len(image) > 500:
                self._last_image = image
                self._last_image_time = time.time()
                _LOGGER.debug(
                    "Snapshot captured for %s (%d bytes)",
                    self._device.name, len(image),
                )
        except Exception:
            _LOGGER.exception("Snapshot capture failed for %s", self._device.name)
        finally:
            self._capturing = False

        return self._last_image

    async def _capture_snapshot(self) -> bytes | None:
        """Server-side WebRTC snapshot using aiortc."""
        try:
            from aiortc import RTCPeerConnection, RTCSessionDescription
        except ImportError:
            _LOGGER.error(
                "aiortc not available — snapshot capture requires aiortc. "
                "It should be installed automatically via requirements."
            )
            return None

        from ring_doorbell.const import (
            APP_API_URI,
            RTC_STREAMING_TICKET_ENDPOINT,
            RTC_STREAMING_WEB_SOCKET_ENDPOINT,
        )

        import json
        import ssl
        import uuid

        from websockets.asyncio.client import connect as ws_connect

        # 1. Get signaling ticket
        try:
            resp = await self._device._ring.async_query(
                RTC_STREAMING_TICKET_ENDPOINT,
                method="POST",
                base_uri=APP_API_URI,
            )
            ticket = resp.json()["ticket"]
        except Exception:
            _LOGGER.debug("Failed to get WebRTC ticket", exc_info=True)
            return None

        # 2. Setup peer connection
        pc = RTCPeerConnection()
        snapshot_data: dict[str, bytes | None] = {"image": None}
        capture_done = asyncio.Event()

        @pc.on("track")
        async def on_track(track):
            if track.kind != "video":
                return

            frame_count = 0
            best_frame = None
            best_brightness = 0.0
            bright_streak = 0
            prev_brightness = 0.0

            try:
                while frame_count < SNAPSHOT_MAX_FRAMES:
                    frame = await asyncio.wait_for(track.recv(), timeout=10)
                    frame_count += 1

                    img = frame.to_image()
                    w, h = img.size
                    # Sample 9 points for brightness
                    points = [
                        (w // 4, h // 4), (w // 2, h // 4), (3 * w // 4, h // 4),
                        (w // 4, h // 2), (w // 2, h // 2), (3 * w // 4, h // 2),
                        (w // 4, 3 * h // 4), (w // 2, 3 * h // 4), (3 * w // 4, 3 * h // 4),
                    ]
                    total = sum(sum(img.getpixel(p)) / 3 for p in points)
                    brightness = total / len(points)

                    if brightness > best_brightness:
                        best_brightness = brightness
                        best_frame = img

                    # Wait for stabilized frame
                    if brightness > SNAPSHOT_BRIGHTNESS_THRESHOLD:
                        bright_streak += 1
                        if (
                            bright_streak >= SNAPSHOT_STABILIZE_FRAMES
                            and prev_brightness > 0
                            and abs(brightness - prev_brightness)
                            < brightness * 0.15
                        ):
                            best_frame = img
                            break
                    else:
                        bright_streak = 0

                    prev_brightness = brightness

            except asyncio.TimeoutError:
                _LOGGER.debug("Frame timeout after %d frames", frame_count)
            except Exception as exc:
                _LOGGER.debug("Frame capture error: %s", exc)

            if best_frame:
                buf = BytesIO()
                best_frame.save(buf, "JPEG", quality=85)
                snapshot_data["image"] = buf.getvalue()

            capture_done.set()

        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # 3. WebSocket signaling
        ws_uri = RTC_STREAMING_WEB_SOCKET_ENDPOINT.format(uuid.uuid4(), ticket)
        dialog_id = str(uuid.uuid4())
        session_id = None

        ssl_ctx = ssl.create_default_context()

        try:
            async with ws_connect(
                ws_uri,
                user_agent_header="android:com.ringapp",
                ssl=ssl_ctx,
            ) as ws:
                await ws.send(json.dumps({
                    "method": "live_view",
                    "dialog_id": dialog_id,
                    "body": {
                        "doorbot_id": self._device.device_api_id,
                        "stream_options": {
                            "audio_enabled": False,
                            "video_enabled": True,
                        },
                        "sdp": pc.localDescription.sdp,
                        "type": "offer",
                    },
                }))

                start = time.time()
                while time.time() - start < 20 and not capture_done.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3)
                        msg = json.loads(raw)
                        method = msg.get("method", "")
                        body = msg.get("body", {})

                        if method == "sdp":
                            sdp = body.get("sdp", "")
                            if sdp:
                                await pc.setRemoteDescription(
                                    RTCSessionDescription(
                                        sdp=sdp, type="answer"
                                    )
                                )
                        elif method == "session_created":
                            session_id = body.get("session_id")
                        elif (
                            method == "notification"
                            and body.get("text") == "camera_connected"
                        ):
                            if session_id:
                                await ws.send(json.dumps({
                                    "method": "activate_session",
                                    "dialog_id": dialog_id,
                                    "body": {
                                        "doorbot_id": self._device.device_api_id,
                                        "session_id": session_id,
                                    },
                                }))
                        elif method == "close":
                            break
                    except asyncio.TimeoutError:
                        if capture_done.is_set():
                            break

                # Clean close
                try:
                    await ws.send(json.dumps({
                        "method": "close",
                        "dialog_id": dialog_id,
                        "body": {
                            "session_id": session_id or "",
                            "reason": {"code": 0, "text": ""},
                        },
                    }))
                except Exception:
                    pass

        except Exception:
            _LOGGER.debug("WebRTC signaling error", exc_info=True)
        finally:
            await pc.close()

        return snapshot_data["image"]

    # ---- Recording (server-side WebRTC capture to MP4) ----

    async def async_record(
        self,
        filename: str,
        duration: int,
        lookback: int = 0,
    ) -> None:
        """Record a video clip from the camera.

        Args:
            filename: Path to save the recorded video file.
            duration: Duration of the recording in seconds.
            lookback: Not supported. Recording starts immediately.

        Raises:
            HomeAssistantError: If recording fails.
        """
        from homeassistant.exceptions import HomeAssistantError

        if self._is_recording:
            raise HomeAssistantError(
                f"Already recording for {self._device.name}"
            )

        if lookback:
            _LOGGER.warning(
                "Lookback is not supported for %s, recording starts immediately",
                self._device.name,
            )

        # Ensure the directory exists
        os.makedirs(os.path.dirname(filename), exist_ok=True)

        _LOGGER.info(
            "Starting recording for %s: %s (%ds)",
            self._device.name, filename, duration,
        )

        self._is_recording = True
        self._recording_file = filename
        self.async_write_ha_state()

        try:
            await self._record_video(duration, filename)
            _LOGGER.info(
                "Recording completed for %s: %s",
                self._device.name, filename,
            )
        except Exception as err:
            _LOGGER.exception("Recording failed for %s", self._device.name)
            raise HomeAssistantError(
                f"Recording failed for {self._device.name}: {err}"
            ) from err
        finally:
            self._is_recording = False
            self._recording_file = None
            self.async_write_ha_state()

    async def _record_video(self, duration: int, filename: str) -> None:
        """Server-side WebRTC recording to MP4 using aiortc + PyAV."""
        try:
            from aiortc import RTCPeerConnection, RTCSessionDescription
        except ImportError:
            _LOGGER.error(
                "aiortc not available — recording requires aiortc. "
                "It should be installed automatically via requirements."
            )
            return

        import av
        from ring_doorbell.const import (
            APP_API_URI,
            RTC_STREAMING_TICKET_ENDPOINT,
            RTC_STREAMING_WEB_SOCKET_ENDPOINT,
        )
        import json
        import ssl
        import uuid
        from websockets.asyncio.client import connect as ws_connect

        # 1. Get signaling ticket
        try:
            resp = await self._device._ring.async_query(
                RTC_STREAMING_TICKET_ENDPOINT,
                method="POST",
                base_uri=APP_API_URI,
            )
            ticket = resp.json()["ticket"]
        except Exception:
            _LOGGER.debug("Failed to get WebRTC ticket", exc_info=True)
            return

        # 2. Setup peer connection with video + audio
        pc = RTCPeerConnection()
        recording_done = asyncio.Event()

        # MP4 container setup
        container = av.open(filename, mode="w")
        video_stream = None
        audio_stream = None

        @pc.on("track")
        async def on_track(track):
            nonlocal video_stream, audio_stream

            if track.kind == "video":
                # Create video stream on first frame to get dimensions
                try:
                    while not recording_done.is_set():
                        frame = await asyncio.wait_for(track.recv(), timeout=5)
                        if video_stream is None:
                            video_stream = container.add_stream(
                                "h264", rate=25
                            )
                            video_stream.width = frame.width
                            video_stream.height = frame.height
                            video_stream.pix_fmt = "yuv420p"
                        for packet in video_stream.encode(frame):
                            container.mux(packet)
                except (asyncio.TimeoutError, Exception):
                    if not recording_done.is_set():
                        _LOGGER.debug("Video track ended or timed out")

            elif track.kind == "audio":
                try:
                    while not recording_done.is_set():
                        frame = await asyncio.wait_for(track.recv(), timeout=5)
                        if audio_stream is None:
                            audio_stream = container.add_stream(
                                "aac", rate=frame.sample_rate
                            )
                            audio_stream.layout = frame.layout.name
                        for packet in audio_stream.encode(frame):
                            container.mux(packet)
                except (asyncio.TimeoutError, Exception):
                    if not recording_done.is_set():
                        _LOGGER.debug("Audio track ended or timed out")

        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        offer = await pc.createOffer()
        await pc.setLocalDescription(offer)

        # 3. WebSocket signaling
        ws_uri = RTC_STREAMING_WEB_SOCKET_ENDPOINT.format(uuid.uuid4(), ticket)
        dialog_id = str(uuid.uuid4())
        session_id = None

        ssl_ctx = ssl.create_default_context()

        try:
            async with ws_connect(
                ws_uri,
                user_agent_header="android:com.ringapp",
                ssl=ssl_ctx,
            ) as ws:
                await ws.send(json.dumps({
                    "method": "live_view",
                    "dialog_id": dialog_id,
                    "body": {
                        "doorbot_id": self._device.device_api_id,
                        "stream_options": {
                            "audio_enabled": True,
                            "video_enabled": True,
                        },
                        "sdp": pc.localDescription.sdp,
                        "type": "offer",
                    },
                }))

                # Wait for duration, monitoring the WebSocket
                start = time.time()
                while time.time() - start < duration:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        msg = json.loads(raw)
                        method = msg.get("method", "")
                        body = msg.get("body", {})

                        if method == "sdp":
                            sdp = body.get("sdp", "")
                            if sdp:
                                await pc.setRemoteDescription(
                                    RTCSessionDescription(sdp=sdp, type="answer")
                                )
                        elif method == "session_created":
                            session_id = body.get("session_id")
                        elif (
                            method == "notification"
                            and body.get("text") == "camera_connected"
                        ):
                            if session_id:
                                await ws.send(json.dumps({
                                    "method": "activate_session",
                                    "dialog_id": dialog_id,
                                    "body": {
                                        "doorbot_id": self._device.device_api_id,
                                        "session_id": session_id,
                                    },
                                }))
                        elif method == "close":
                            break
                    except asyncio.TimeoutError:
                        # Normal — just check if duration elapsed
                        pass

                # Duration reached — stop recording
                recording_done.set()

                # Flush remaining frames
                if video_stream:
                    for packet in video_stream.encode():
                        container.mux(packet)
                if audio_stream:
                    for packet in audio_stream.encode():
                        container.mux(packet)

                # Close the container
                container.close()

                # Clean close WebSocket
                try:
                    await ws.send(json.dumps({
                        "method": "close",
                        "dialog_id": dialog_id,
                        "body": {
                            "session_id": session_id or "",
                            "reason": {"code": 0, "text": ""},
                        },
                    }))
                except Exception:
                    pass

        except Exception:
            _LOGGER.debug("WebRTC recording signaling error", exc_info=True)
            # Make sure container is closed even on error
            try:
                container.close()
            except Exception:
                pass
        finally:
            await pc.close()

    # ---- Live stream (browser WebRTC signaling bridge) ----

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle WebRTC offer from the HA frontend."""
        def _message_wrapper(ring_msg: RingWebRtcMessage) -> None:
            if ring_msg.error_code:
                msg = ring_msg.error_message or ""
                send_message(WebRTCError(ring_msg.error_code, msg))
            elif ring_msg.answer:
                send_message(WebRTCAnswer(ring_msg.answer))
            elif ring_msg.candidate:
                send_message(
                    WebRTCCandidate(
                        RTCIceCandidateInit(
                            ring_msg.candidate,
                            sdp_m_line_index=ring_msg.sdp_m_line_index or 0,
                        )
                    )
                )

        await self._device.generate_async_webrtc_stream(
            offer_sdp, session_id, _message_wrapper, keep_alive_timeout=None
        )

    async def async_on_webrtc_candidate(
        self, session_id: str, candidate: RTCIceCandidateInit
    ) -> None:
        """Forward an ICE candidate from the browser to Ring."""
        if candidate.sdp_m_line_index is None:
            _LOGGER.warning("ICE candidate without sdp_m_line_index, ignoring")
            return
        await self._device.on_webrtc_candidate(
            session_id, candidate.candidate, candidate.sdp_m_line_index
        )

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session."""
        self._device.sync_close_webrtc_stream(session_id)

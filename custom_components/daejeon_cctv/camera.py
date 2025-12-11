"""Camera platform for Daejeon CCTV integration."""
from __future__ import annotations

import asyncio
import logging
import ssl
import time
from pathlib import Path
from datetime import datetime
from aiohttp import web

import aiohttp
import aiofiles
from bs4 import BeautifulSoup

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.components.http import HomeAssistantView

from .const import (
    CONF_CCTV_NAME,
    CONF_CCTV_URL,
    CONF_MJPEG_HOST,
    CONF_MJPEG_PORT,
    CONF_MJPEG_EXTERNAL_URL,
    DEFAULT_MJPEG_HOST,
    DEFAULT_MJPEG_PORT,
    DEFAULT_MJPEG_EXTERNAL_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Create SSL context at module level to avoid blocking I/O in async context
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# Global camera registry
_CAMERAS = {}
_MJPEG_SERVER = None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Daejeon CCTV camera platform."""
    global _MJPEG_SERVER
    
    cctv_url = config_entry.data[CONF_CCTV_URL]
    cctv_name = config_entry.data[CONF_CCTV_NAME]
    mjpeg_host = config_entry.data.get(CONF_MJPEG_HOST, DEFAULT_MJPEG_HOST)
    mjpeg_port = config_entry.data.get(CONF_MJPEG_PORT, DEFAULT_MJPEG_PORT)
    mjpeg_external_url = config_entry.data.get(CONF_MJPEG_EXTERNAL_URL, DEFAULT_MJPEG_EXTERNAL_URL)

    camera = DaejeonCCTVCamera(hass, cctv_url, cctv_name)
    
    # Generate camera ID for registry
    camera_id = camera.get_camera_id()
    _CAMERAS[camera_id] = camera
    
    # Store camera_id and MJPEG config in hass.data
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    if "cameras" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["cameras"] = {}
    hass.data[DOMAIN]["cameras"][cctv_name] = {
        "camera_id": camera_id,
        "cctv_url": cctv_url,
        "mjpeg_host": mjpeg_host,
        "mjpeg_port": mjpeg_port,
        "mjpeg_external_url": mjpeg_external_url,
    }
    
    _LOGGER.info("Camera %s registered with ID: %s", cctv_name, camera_id)
    _LOGGER.info("MJPEG URL for external access: %s/mjpeg/%s", mjpeg_external_url, camera_id)
    
    # Store MJPEG config for later use
    camera._mjpeg_host = mjpeg_host
    camera._mjpeg_port = mjpeg_port
    camera._mjpeg_external_url = mjpeg_external_url
    
    # Register video file view once
    if not hasattr(hass.data.get(DOMAIN, {}), 'views_registered'):
        hass.http.register_view(DaejeonCCTVVideoView())
        hass.http.register_view(DaejeonCCTVHLSView())
        hass.data[DOMAIN]['views_registered'] = True
    
    # Start MJPEG server if not already running with same config
    if _MJPEG_SERVER is None:
        _MJPEG_SERVER = MJPEGServer(hass, host=mjpeg_host, port=mjpeg_port)
        await _MJPEG_SERVER.start()
        hass.data[DOMAIN]['mjpeg_server'] = _MJPEG_SERVER
    
    async_add_entities([camera], True)


class MJPEGServer:
    """Standalone MJPEG server for HA MJPEG IP Camera integration."""

    def __init__(self, hass: HomeAssistant, host: str = "0.0.0.0", port: int = 8899) -> None:
        """Initialize MJPEG server."""
        self._hass = hass
        self._host = host
        self._port = port
        self._runner = None
        self._site = None

    async def start(self) -> None:
        """Start the MJPEG server."""
        app = web.Application()
        app.router.add_get("/mjpeg/{camera_id}", self._handle_mjpeg)
        
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        _LOGGER.info("MJPEG server started on %s:%d", self._host, self._port)

    async def stop(self) -> None:
        """Stop the MJPEG server."""
        if self._site:
            await self._site.stop()
        if self._runner:
            await self._runner.cleanup()
        _LOGGER.info("MJPEG server stopped")

    async def _handle_mjpeg(self, request: web.Request) -> web.StreamResponse:
        """Handle MJPEG stream request, auto-advancing through videos."""
        camera_id = request.match_info.get("camera_id")
        camera = _CAMERAS.get(camera_id)
        
        if not camera:
            return web.Response(status=404, text="Camera not found")

        _LOGGER.debug("MJPEG stream requested for camera %s", camera_id)
        
        boundary = b"--boundary"
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "multipart/x-mixed-replace; boundary=--boundary",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)

        stream_start_time = time.time()
        current_video_start_time = stream_start_time
        current_video_path = None
        process = None
        chunk_size = 4096
        last_chunk_time = time.time()
        timeout = 30
        buffer = bytearray()

        try:
            while True:
                # If process/stdout missing, wait briefly
                if process is None or process.stdout is None:
                    await asyncio.sleep(0.1)
                    continue

                # Get latest video
                latest_video = camera._get_latest_video()
                if not latest_video:
                    await asyncio.sleep(0.5)
                    continue
                
                # If current video changed or never set, start playing new one
                if current_video_path != latest_video:
                    current_video_path = latest_video
                    current_video_start_time = time.time()
                    
                    # Kill old process if running
                    if process:
                        try:
                            process.terminate()
                            await process.wait()
                        except:
                            pass
                    
                    # Start ffmpeg with new video, emit MJPEG frames to stdout
                    cmd = [
                        "ffmpeg",
                        "-loglevel", "quiet",
                        "-nostats",
                        "-i", str(current_video_path),
                        "-c:v", "mjpeg",
                        "-q:v", "3",
                        "-vf", "fps=15",
                        "-f", "image2pipe",
                        "pipe:1",
                    ]
                    
                    process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    _LOGGER.debug("MJPEG: Started playback of %s", current_video_path.name)
                
                # Read from ffmpeg
                stdout = process.stdout
                if stdout is None:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    chunk = await asyncio.wait_for(
                        stdout.read(chunk_size),
                        timeout=5.0,
                    )
                    if not chunk:
                        await asyncio.sleep(0.1)
                        continue

                    buffer.extend(chunk)

                    # Extract frames by JPEG end marker
                    while True:
                        end_idx = buffer.find(b"\xff\xd9")
                        if end_idx == -1:
                            break
                        frame = bytes(buffer[: end_idx + 2])
                        del buffer[: end_idx + 2]

                        part_header = (
                            boundary
                            + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                            + str(len(frame)).encode()
                            + b"\r\n\r\n"
                        )
                        try:
                            await response.write(part_header + frame + b"\r\n")
                        except ConnectionResetError:
                            break
                        last_chunk_time = time.time()

                except asyncio.IncompleteReadError:
                    # Video ended, loop will pick up next one
                    continue
                except asyncio.TimeoutError:
                    if time.time() - last_chunk_time > timeout:
                        _LOGGER.warning("MJPEG stream timeout for camera %s", camera_id)
                        break
        except Exception as e:
            _LOGGER.error("Error in MJPEG stream: %s", e)
        finally:
            if process:
                try:
                    process.terminate()
                    await process.wait()
                except:
                    pass
            if response.prepared:
                try:
                    await response.write_eof()
                except ConnectionResetError:
                    pass

        return response

class DaejeonCCTVVideoView(HomeAssistantView):
    """Serve downloaded video files."""

    url = r"/api/daejeon_cctv/{camera_id}/video/{timestamp:\d+}/{filename:[^?]+\.mp4}"
    name = "api:daejeon_cctv:video"
    requires_auth = False

    async def get(self, request, camera_id, filename, timestamp):
        _LOGGER.info("Video file requested: camera_id=%s, filename=%s", camera_id, filename)
        camera = _CAMERAS.get(camera_id)
        if not camera or not camera._video_dir:
            return web.Response(status=404, text="Camera not found")

        # Strip query params if they're in the filename (shouldn't be, but be safe)
        if '?' in filename:
            filename = filename.split('?')[0]

        file_path = camera._video_dir / filename
        if not file_path.exists() or not file_path.name.endswith(".mp4"):
            return web.Response(status=404, text="Video not found")

        # Check if video is old enough (at least 5 seconds since download completed)
        if filename in camera._video_ready_time:
            ready_time = camera._video_ready_time[filename]
            age = time.time() - ready_time
            if age < 5.0:
                _LOGGER.warning("Video too fresh (age=%.1fs), rejecting: %s", age, filename)
                return web.Response(status=503, text="Video not ready yet")

        file_size = file_path.stat().st_size
        _LOGGER.debug("Serving video: %s (size: %d bytes)", filename, file_size)
        
        # Build headers with explicit Content-Length to prevent buffering
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(file_size),
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Accept-Ranges": "bytes",
        }
        
        # Support HTTP range requests for seeking/scrubbing
        return web.FileResponse(
            path=file_path,
            headers=headers,
        )


class DaejeonCCTVHLSView(HomeAssistantView):
    """Serve generated HLS playlists and segments."""

    url = "/api/daejeon_cctv/{camera_id}/hls/{filename}"
    name = "api:daejeon_cctv:hls"
    requires_auth = False

    async def get(self, request, camera_id, filename):
        camera = _CAMERAS.get(camera_id)
        if not camera:
            return web.Response(status=404, text="Camera not found")

        # Update last access time to keep download loop running
        camera._last_access_time = time.time()

        hls_dir = camera._get_hls_dir()
        if not hls_dir:
            return web.Response(status=503, text="HLS not initialized yet")

        # Determine which entry to serve from
        # Priority: any ready entry (prefer active) > active entry (even if not ready)
        serving_entry_id = None
        
        # Collect all ready entries
        ready_entries = [
            (eid, entry) for eid, entry in camera._hls_entries.items()
            if entry["ready_time"] is not None
        ]
        
        if ready_entries:
            # Prefer active entry if it's ready
            active_ready = [(eid, entry) for eid, entry in ready_entries if eid == camera._hls_active_entry_id]
            if active_ready:
                serving_entry_id = active_ready[0][0]
                _LOGGER.debug("Serving active ready HLS entry %d", serving_entry_id)
            else:
                # Use newest ready entry
                ready_entries.sort(key=lambda x: x[1]["ready_time"], reverse=True)
                serving_entry_id = ready_entries[0][0]
                _LOGGER.debug("Serving newest ready HLS entry %d", serving_entry_id)
                
                # Debug: show if there's a newer active entry building
                if camera._hls_active_entry_id is not None and camera._hls_active_entry_id != serving_entry_id:
                    active_entry = camera._hls_entries.get(camera._hls_active_entry_id)
                    if active_entry:
                        active_size = active_entry.get("fileSize", 0)
                        active_process = active_entry.get("process")
                        process_status = "running" if active_process and active_process.returncode is None else "dead"
                        _LOGGER.debug("Newer entry %d is building (size=%d, not ready yet, process: %s)", 
                                    camera._hls_active_entry_id, active_size, process_status)
                        
                        # If process is dead, log stderr
                        if active_process and active_process.returncode is not None and active_process.stderr:
                            try:
                                stderr_data = await active_process.stderr.read()
                                if stderr_data:
                                    _LOGGER.error("Entry %d process died with stderr: %s", 
                                                camera._hls_active_entry_id, stderr_data.decode())
                            except:
                                pass
        else:
            # No ready entries - fall back to active entry even if not ready
            if camera._hls_active_entry_id is not None:
                active_entry = camera._hls_entries.get(camera._hls_active_entry_id)
                if active_entry:
                    serving_entry_id = camera._hls_active_entry_id
                    active_size = active_entry.get("fileSize", 0)
                    _LOGGER.debug("Using building active entry %d (size=%d)", serving_entry_id, active_size)
        
        # Resolve file path based on serving entry
        file_path = hls_dir / filename
        playlist_filename = filename
        
        if serving_entry_id is not None:
            entry = camera._hls_entries[serving_entry_id]
            entry_dir = entry.get("entry_dir")
            
            # Handle requests without entry prefix - use serving entry's directory
            if filename == "index.m3u8":
                # Legacy index.m3u8 -> use entry's playlist
                playlist_filename = entry["playlist"]
                file_path = hls_dir / playlist_filename
                _LOGGER.debug("Using playlist %s for entry %d", playlist_filename, serving_entry_id)
            elif filename.endswith(".ts") and "/" not in filename:
                # Segment without prefix -> use serving entry's directory
                if entry_dir:
                    file_path = entry_dir / filename
                    _LOGGER.debug("Using segment %s from entry %d", filename, serving_entry_id)
        
        # If file doesn't exist, return appropriate error
        if not file_path.exists():
            # Check if it's a playlist or segment
            is_playlist = filename.endswith(".m3u8") or playlist_filename.endswith(".m3u8")
            if is_playlist:
                _LOGGER.warning("HLS playlist %s not found yet, retrying... (hls_dir=%s)", playlist_filename, hls_dir)
                return web.Response(status=202, text="HLS packaging in progress")
            else:
                _LOGGER.debug("HLS segment not found: %s", file_path)
                return web.Response(status=404, text="HLS segment not found")

        # For playlist file, verify it has valid content
        is_playlist = filename.endswith(".m3u8") or playlist_filename.endswith(".m3u8")
        if is_playlist:
            try:
                file_size = file_path.stat().st_size
                if file_size == 0:
                    _LOGGER.warning("HLS playlist exists but is empty; returning 202 to retry")
                    return web.Response(status=202, text="HLS playlist empty; retrying")
                async with aiofiles.open(file_path, 'r') as f:
                    content = await f.read()
                has_extinf = "#EXTINF" in content
                if not has_extinf:
                    _LOGGER.warning("HLS playlist missing segments; returning 202 to retry")
                    return web.Response(status=202, text="HLS playlist not ready; retrying")
                first_line = content.splitlines()[0] if content else ""

                # Update fileSize and ready_time for the entry being served (not just active)
                if serving_entry_id is not None:
                    entry = camera._hls_entries.get(serving_entry_id)
                    if entry:
                        # Always update fileSize for the served entry
                        entry["fileSize"] = file_size
                        if entry["ready_time"] is None and file_size > 1000:
                            entry["ready_time"] = time.time()
                            _LOGGER.info("HLS entry %d marked ready (size=%d)", serving_entry_id, file_size)
                        elif entry["ready_time"] is None:
                            _LOGGER.info("HLS entry %d not ready yet (size=%d, need >1000)", serving_entry_id, file_size)

                # If we're serving a non-ready entry, return 202
                if serving_entry_id is not None:
                    entry = camera._hls_entries.get(serving_entry_id)
                    if entry and entry["ready_time"] is None:
                        return web.Response(status=202, text="HLS not ready yet (building up segments)")

                _LOGGER.info("Serving HLS playlist %s: size=%d bytes, first_line=%s", playlist_filename, file_size, first_line)
            except Exception as e:
                _LOGGER.error("Error reading HLS playlist %s: %s", file_path, e)
                return web.Response(status=500, text=f"Error reading playlist: {e}")
        else:
            # For segments, just log size
            try:
                file_size = file_path.stat().st_size
                _LOGGER.debug("Serving HLS segment %s: size=%d bytes", filename, file_size)
            except Exception as e:
                _LOGGER.error("Error accessing HLS segment %s: %s", file_path, e)
                return web.Response(status=500, text=f"Error accessing segment: {e}")

        # Content type hint
        headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
        return web.FileResponse(path=file_path, headers=headers)


class DaejeonCCTVCamera(Camera):
    """Representation of a Daejeon CCTV Camera."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hass: HomeAssistant, cctv_url: str, cctv_name: str) -> None:
        """Initialize the camera."""
        super().__init__()
        self._hass = hass
        self._cctv_url = cctv_url
        self._attr_name = cctv_name
        self._attr_unique_id = f"daejeon_cctv_{cctv_url}"
        self._attr_brand = "Daejeon City"
        self._attr_model = "UTIC CCTV"
        self._video_url: str | None = None
        self._session = async_get_clientsession(hass)
        self._attr_is_on = True
        self._fetch_error: str | None = None
        self._video_dir: Path | None = None
        self._refresh_task: asyncio.Task | None = None
        self._last_access_time: float = 0
        self._idle_timeout = 60
        self._max_videos = 30  # Keep more videos to prevent deletion during streaming
        self._hls_current_video: Path | None = None
        # HLS entries: id -> {"hls": path, "switch_time": time|None, "ready_time": time|None, "process": Process, ...}
        self._hls_entries: dict[int, dict] = {}
        self._hls_next_id = 1
        self._hls_active_entry_id: int | None = None  # Track which entry is currently being written to
        self._hls_source_url: str | None = None  # Track source URL for HLS to detect changes
        # Cache camera_id to avoid repeated hash computation
        import hashlib
        self._camera_id = hashlib.md5(cctv_url.encode()).hexdigest()[:8]
        # Cache latest video to avoid repeated glob() calls on stream requests
        self._cached_latest_video: Path | None = None
        self._cache_time = 0
        # Track which video is currently being served to prevent deletion
        self._serving_video: Path | None = None
        # Track download completion time to prevent serving incomplete files
        self._video_ready_time: dict[str, float] = {}  # filename -> ready_time
        # Cache last snapshot to reduce ffmpeg calls for camera_proxy
        self._snapshot_cache_bytes: bytes | None = None
        self._snapshot_cache_video: Path | None = None
        self._snapshot_cache_time: float = 0

    def get_camera_id(self) -> str:
        """Return cached camera ID."""
        return self._camera_id

    @property
    def use_stream_for_stills(self) -> bool:
        """Prefer generating stills via ffmpeg snapshot to avoid stream dependency."""
        return False

    def camera_image(self, width: int = 640, height: int = 480) -> bytes | None:
        """Sync fallback is unused; HA will call async_camera_image."""
        return None

    async def async_camera_image(self, width: int = 640, height: int = 480) -> bytes | None:
        """Return a JPEG snapshot from the latest ready video to satisfy camera_proxy."""
        self._last_access_time = time.time()
        self._start_refresh_task()

        latest_video = self._get_latest_video_fresh()
        if not latest_video or latest_video.name not in self._video_ready_time:
            _LOGGER.debug("No ready video for snapshot")
            raise ValueError("No video ready for snapshot")

        # Reuse cached snapshot if same video and recent (2s)
        now = time.time()
        if (
            self._snapshot_cache_video is not None
            and self._snapshot_cache_video == latest_video
            and (now - self._snapshot_cache_time) < 2
            and self._snapshot_cache_bytes is not None
        ):
            return self._snapshot_cache_bytes

        # Build ffmpeg command to grab one frame; scale down to requested width if provided
        vf_filter = None
        if width and width > 0:
            vf_filter = f"scale='min({width},iw)':-1"

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-i",
            str(latest_video),
            "-vframes",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
        ]

        if vf_filter:
            cmd.extend(["-vf", vf_filter])

        cmd.append("-")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode != 0:
                _LOGGER.warning("Snapshot ffmpeg failed (code %s): %s", proc.returncode, stderr.decode(errors="ignore"))
                raise ValueError("Snapshot generation failed")
            if not stdout:
                _LOGGER.warning("Snapshot ffmpeg produced no data")
                raise ValueError("Snapshot ffmpeg produced no data")
            # Cache snapshot
            self._snapshot_cache_video = latest_video
            self._snapshot_cache_bytes = stdout
            self._snapshot_cache_time = time.time()
            return stdout
        except asyncio.TimeoutError:
            _LOGGER.warning("Snapshot ffmpeg timed out")
            raise ValueError("Snapshot generation timeout")
        except Exception as err:
            _LOGGER.error("Snapshot error: %s", err)
            return None

    async def async_get_stream_source(self) -> str | None:
        """Return HLS stream URL; wait for playlist to be valid before returning.
        
        Always return a URL so HA doesn't stall, but ensure playlist exists
        and has content before giving it to HA.
        """
        self._last_access_time = time.time()
        self._start_refresh_task()  # Ensure download task is running

        # Get latest available video (any that exists and is marked ready)
        latest_video = None
        if self._video_dir:
            videos = sorted(self._video_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
            for video in reversed(videos):
                if video.exists() and video.name in self._video_ready_time:
                    latest_video = video
                    break
        
        if not latest_video:
            # No video yet, but still return a placeholder URL so HA can start polling
            _LOGGER.debug("No video ready yet for %s, returning placeholder URL", self._attr_name)
            camera_id = self.get_camera_id()
            base_url = get_url(self._hass)
            timestamp = int(time.time() * 1000)
            return f"{base_url}/api/daejeon_cctv/{camera_id}/hls/index.m3u8?ts={timestamp}"

        # Track which video is being served
        self._serving_video = latest_video
        _LOGGER.info("Setting serving video: %s", latest_video.name)

        # Start HLS packaging immediately
        if self._hls_current_video is None or self._hls_current_video != latest_video:
            await self._stop_hls()  # Stop old one first for initial stream setup
            await self._start_hls_live(latest_video)
            self._hls_current_video = latest_video
            _LOGGER.debug("HLS repack started for %s", latest_video.name)

        camera_id = self.get_camera_id()
        base_url = get_url(self._hass)
        timestamp = int(time.time() * 1000)

        hls_url = f"{base_url}/api/daejeon_cctv/{camera_id}/hls/index.m3u8?ts={timestamp}"
        _LOGGER.info("Stream source URL (HLS): %s", hls_url)
        return hls_url

    async def stream_source(self) -> str | None:
        """HA play_stream compatibility wrapper."""
        return await self.async_get_stream_source()

    async def async_added_to_hass(self) -> None:
        """Start background refresh immediately so first video is ready sooner."""
        await super().async_added_to_hass()
        self._last_access_time = time.time()
        self._start_refresh_task()

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        await super().async_will_remove_from_hass()
        await self._stop_hls()
        self._stop_refresh_task()

    def _stop_refresh_task(self) -> None:
        """Stop the refresh task if it's running."""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            self._refresh_task = None
            _LOGGER.debug("Stopped refresh task for %s", self._attr_name)

    def _start_refresh_task(self) -> None:
        """Start the refresh task if not already running."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self._hass.loop.create_task(self._download_loop())
            _LOGGER.debug("Started refresh task for %s", self._attr_name)

    async def _fetch_video_url(self) -> str | None:
        """Fetch the video source URL from the CCTV page."""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
            }
            
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(self._cctv_url, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        _LOGGER.error("Failed to fetch CCTV page: %s", response.status)
                        return None
                    
                    html = await response.text()
                    _LOGGER.info("Fetched CCTV HTML: %d bytes", len(html))
                    
                    # Look for URLs matching pattern: https://tportal.daejeon.go.kr...mp4
                    import re
                    urls = re.findall(r'https://tportal\.daejeon\.go\.kr[^\s"\'<>]*\.mp4', html)
                    if urls:
                        video_url = urls[0]
                        _LOGGER.info("Found video URL: %s", video_url)
                        return video_url
                    
                    # Try broader regex: any https://.mp4 URL
                    urls = re.findall(r'https://[^\s"<>]*\.mp4', html)
                    if urls:
                        video_url = urls[0]
                        _LOGGER.info("Found video URL (broader): %s", video_url)
                        return video_url
                    
                    # Log first 2000 chars to debug
                    _LOGGER.debug("CCTV HTML snippet: %s", html[:2000])
                    
                    # Fallback: Look for video tag with source
                    soup = BeautifulSoup(html, "html.parser")
                    video_tag = soup.find("video")
                    if video_tag:
                        source = video_tag.find("source")
                        if source and source.get("src"):
                            video_url = source.get("src")
                            if video_url and not video_url.startswith("http"):
                                video_url = f"https://www.utic.go.kr{video_url}"
                            _LOGGER.info("Found video URL from tag: %s", video_url)
                            return video_url
                    
                    _LOGGER.warning("No video source found")
                    return None
                
        except Exception as e:
            _LOGGER.error("Error fetching video URL: %s", e)
            return None

    async def _download_and_save_video(self) -> bool:
        """Download video from source and save it."""
        if not self._video_url:
            return False

        try:
            # Prepare video directory
            base_dir = Path("/tmp/daejeon_cctv")
            base_dir.mkdir(parents=True, exist_ok=True)
            self._video_dir = base_dir / self.get_camera_id()
            self._video_dir.mkdir(parents=True, exist_ok=True)

            # Download video with timeout
            _LOGGER.info("Downloading video from %s", self._video_url)
            async with self._session.get(self._video_url, timeout=30) as response:
                if response.status != 200:
                    _LOGGER.error("Failed to download video: %s", response.status)
                    return False

                # Save with timestamp
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                file_path = self._video_dir / f"video_{timestamp}.mp4"
                
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(65536):
                        await f.write(chunk)

                _LOGGER.info("Saved video to %s", file_path)
                # Mark video as ready and clean up old videos
                self._video_ready_time[file_path.name] = time.time()
                self._cleanup_old_videos()
                self._fetch_error = None
                return True

        except Exception as e:
            _LOGGER.error("Error downloading video: %s", e)
            self._fetch_error = str(e)
            return False

    def _cleanup_old_videos(self) -> None:
        """Keep the newest 30 videos, plus protect currently-serving video and HLS source video."""
        if not self._video_dir:
            return

        videos = sorted(self._video_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
        current_time = time.time()
        
        # Keep many videos (30) to ensure streaming videos aren't deleted
        if len(videos) > self._max_videos:
            for video in videos[:-self._max_videos]:
                # Don't delete the video that's currently being served
                if self._serving_video:
                    try:
                        if video.samefile(self._serving_video):
                            _LOGGER.debug("Keeping in-use video: %s", video.name)
                            continue
                    except (OSError, ValueError):
                        pass
                
                # Don't delete the video being HLS-packaged (ffmpeg is reading it)
                if self._hls_current_video:
                    try:
                        if video.samefile(self._hls_current_video):
                            _LOGGER.debug("Keeping HLS source video: %s", video.name)
                            continue
                    except (OSError, ValueError):
                        pass
                
                # Don't delete videos accessed in last 30 seconds (still streaming)
                if video.name in self._video_ready_time:
                    ready_time = self._video_ready_time[video.name]
                    age = current_time - ready_time
                    if age < 30:  # Keep for 30 seconds after download completes
                        _LOGGER.debug("Keeping recently-ready video: %s (age=%.1fs)", video.name, age)
                        continue
                
                try:
                    video.unlink()
                    _LOGGER.debug("Deleted old video: %s", video.name)
                except OSError:
                    pass
        
        # Clean up old ready_time entries for deleted videos
        existing_files = {v.name for v in videos}
        stale_entries = [k for k in self._video_ready_time.keys() if k not in existing_files]
        for entry in stale_entries:
            del self._video_ready_time[entry]
        
        # Invalidate cache after cleanup (new video available)
        self._cached_latest_video = None
        self._cache_time = 0

    def _get_latest_video(self) -> Path | None:
        """Get the newest video file (cached to avoid blocking glob calls).
        
        Used by MJPEG server - 2 second cache is fine here.
        """
        if not self._video_dir:
            return None

        # Cache for 2 seconds to avoid repeated glob() on every stream request
        current_time = time.time()
        if self._cached_latest_video and (current_time - self._cache_time) < 2:
            # Verify cached video still exists
            if self._cached_latest_video.exists():
                return self._cached_latest_video
            else:
                # Cache stale, clear it
                self._cached_latest_video = None
                self._cache_time = 0
        
        videos = sorted(self._video_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
        self._cached_latest_video = videos[-1] if videos else None
        self._cache_time = current_time
        return self._cached_latest_video

    def _get_latest_video_fresh(self) -> Path | None:
        """Get the newest video file (fresh lookup, no cache).
        
        Used by stream source - always returns latest for streaming.
        """
        if not self._video_dir:
            return None
        
        videos = sorted(self._video_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
        latest = videos[-1] if videos else None
        if latest:
            _LOGGER.debug("Latest video (fresh): %s", latest.name)
        return latest

    def _get_hls_dir(self) -> Path | None:
        """Return the HLS output directory for this camera."""
        if not self._video_dir:
            return None
        return self._video_dir / "hls"

    async def _stop_hls(self) -> None:
        """Stop all running HLS ffmpeg processes."""
        for hls_id, entry in list(self._hls_entries.items()):
            process = entry.get("process")
            if process:
                try:
                    process.terminate()
                    await process.wait()
                    _LOGGER.debug("Stopped HLS process for entry %d", hls_id)
                except Exception as e:
                    _LOGGER.debug("Error stopping HLS process %d: %s", hls_id, e)
        self._hls_entries.clear()

    async def _start_hls_live(self, video_path: Path) -> int:
        """Start/replace a looping HLS packager for the given MP4 (omit ENDLIST).
        
        Returns the HLS entry ID.
        """
        hls_dir = self._get_hls_dir()
        if not hls_dir:
            return -1
        hls_dir.mkdir(parents=True, exist_ok=True)

        # Create entry for this HLS with process reference
        hls_id = self._hls_next_id
        self._hls_next_id += 1
        
        # Use unique subdirectory for this entry's files
        entry_dir = hls_dir / f"entry{hls_id}"
        entry_dir.mkdir(parents=True, exist_ok=True)
        
        # Playlist file in entry subdirectory
        playlist_filename = f"entry{hls_id}/index.m3u8"
        
        self._hls_entries[hls_id] = {
            "hls": video_path,
            "switch_time": None,
            "ready_time": None,
            "process": None,  # Will be set after process starts
            "fileSize": 0,  # Track playlist size for readiness decisions
            "playlist": playlist_filename,  # Track which playlist file this entry uses
            "entry_dir": entry_dir,  # Track entry directory for cleanup
        }
        self._hls_active_entry_id = hls_id  # Mark this as the active entry
        _LOGGER.info("Created HLS entry %d for %s (dir: entry%d)", hls_id, video_path.name, hls_id)

        # Each entry starts segment numbering from 0 (isolated in its own directory)
        start_number = 0

        playlist_path = hls_dir / playlist_filename
        segment_pattern = str(entry_dir / "segment_%03d.ts")

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-re",
            "-stream_loop",
            "-1",
            "-i",
            str(video_path),
            "-c:v",
            "copy",
            "-c:a",
            "copy",
            "-hls_time",
            "4",
            "-hls_list_size",
            "15",
            "-hls_segment_type",
            "mpegts",
            "-hls_flags",
            "delete_segments+omit_endlist+program_date_time+discont_start",
            "-start_number",
            str(start_number),
            "-hls_segment_filename",
            segment_pattern,
            "-f",
            "hls",
            str(playlist_path),
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            # Only track process in the entry, not in self._hls_process (allows multiple concurrent processes)
            self._hls_entries[hls_id]["process"] = process
            _LOGGER.debug("HLS packager started for %s (entry %d, segment start: %d, pid: %s)", 
                         self._attr_name, hls_id, start_number, process.pid)
            
            # Check if process is still running after a brief moment
            await asyncio.sleep(0.1)
            if process.returncode is not None:
                stderr = await process.stderr.read() if process.stderr else b''
                _LOGGER.error("HLS process %d died immediately: %s", hls_id, stderr.decode())
                self._hls_entries.pop(hls_id, None)
                return -1
        except Exception as err:
            _LOGGER.error("Error starting HLS for %s: %s", self._attr_name, err)
            self._hls_entries.pop(hls_id, None)
            return -1
        
        return hls_id

    async def _cleanup_old_hls_entries(self) -> None:
        """Clean up old HLS entries that were switched away from (runs slowly)."""
        cutoff_time = time.time() - 180  # Keep entries for 3 minutes after switch (increased from 2)
        to_delete = []
        
        # Find ready entries to keep at least 2 as fallback (current + 1 previous)
        ready_entries = [
            (hls_id, entry) for hls_id, entry in self._hls_entries.items()
            if entry.get("ready_time") is not None
        ]
        ready_entries.sort(key=lambda x: x[1]["ready_time"], reverse=True)
        keep_ready_ids = {ready_entries[i][0] for i in range(min(2, len(ready_entries)))}
        
        for hls_id, entry in self._hls_entries.items():
            switch_time = entry.get("switch_time")
            # Don't delete the latest 2 ready entries even if they're past cutoff
            if hls_id in keep_ready_ids:
                _LOGGER.debug("Keeping ready HLS entry %d as fallback", hls_id)
                continue
            if switch_time is not None and switch_time < cutoff_time:
                to_delete.append(hls_id)
        
        for hls_id in to_delete:
            entry = self._hls_entries[hls_id]
            
            # Delete the entire entry directory (playlist + segments)
            entry_dir = entry.get("entry_dir")
            if entry_dir and entry_dir.exists():
                try:
                    import shutil
                    shutil.rmtree(entry_dir)
                    _LOGGER.info("Deleted entry directory for HLS entry %d", hls_id)
                except OSError as e:
                    _LOGGER.debug("Error deleting entry directory %s: %s", entry_dir, e)
            
            del self._hls_entries[hls_id]
            _LOGGER.info("Cleaned up old HLS entry %d", hls_id)
        
        # Clean up orphaned entry* directories not tracked in entries
        hls_dir = self._get_hls_dir()
        if hls_dir and hls_dir.exists():
            tracked_dirs = {entry.get("entry_dir") for entry in self._hls_entries.values() if entry.get("entry_dir")}
            try:
                for entry_dir in hls_dir.glob("entry*"):
                    if entry_dir.is_dir() and entry_dir not in tracked_dirs:
                        try:
                            import shutil
                            shutil.rmtree(entry_dir)
                            _LOGGER.info("Deleted orphaned entry directory %s", entry_dir.name)
                        except OSError as e:
                            _LOGGER.debug("Error deleting orphaned directory %s: %s", entry_dir.name, e)
            except Exception as e:
                _LOGGER.debug("Error cleaning up orphaned directories: %s", e)

    def _cleanup_videos_not_in_hls(self) -> None:
        """Delete videos that are not referenced by active/ready HLS entries or serving video.

        Keeps newest videos and any that are currently being served or referenced by HLS entries.
        """
        if not self._video_dir:
            return

        keep_paths: set[Path] = set()
        # Keep serving and current HLS source
        if self._serving_video:
            keep_paths.add(self._serving_video)
        if self._hls_current_video:
            keep_paths.add(self._hls_current_video)
        # Keep any videos referenced by HLS entries
        for entry in self._hls_entries.values():
            hls_video = entry.get("hls")
            if hls_video:
                keep_paths.add(hls_video)

        videos = sorted(self._video_dir.glob("video_*.mp4"), key=lambda p: p.stat().st_mtime)
        # Keep newest self._max_videos regardless
        to_consider = videos[:-self._max_videos] if len(videos) > self._max_videos else []

        for video in to_consider:
            if video in keep_paths:
                continue
            # Skip very recent downloads (<30s) to avoid deleting fresh files
            if video.name in self._video_ready_time:
                age = time.time() - self._video_ready_time[video.name]
                if age < 30:
                    continue
            try:
                video.unlink()
                _LOGGER.info("Deleted old video not in HLS: %s", video.name)
            except OSError:
                pass

    async def _wait_for_active_hls_ready(self, timeout: float = 20.0) -> bool:
        """Wait until the active HLS playlist is ready (size > 1000 bytes and has segments)."""
        hls_dir = self._get_hls_dir()
        if not hls_dir:
            return False
        playlist_path = hls_dir / "index.m3u8"

        start = time.time()
        while time.time() - start < timeout:
            if playlist_path.exists():
                try:
                    size = playlist_path.stat().st_size
                    if size > 1000:
                        async with aiofiles.open(playlist_path, "r") as f:
                            content = await f.read()
                        if "#EXTINF" in content:
                            # Mark active entry as ready
                            if self._hls_active_entry_id is not None and self._hls_active_entry_id in self._hls_entries:
                                entry = self._hls_entries[self._hls_active_entry_id]
                                if entry["ready_time"] is None:
                                    entry["ready_time"] = time.time()
                                    _LOGGER.info("HLS entry %d marked ready in wait (size=%d)", self._hls_active_entry_id, size)
                            return True
                except Exception as err:
                    _LOGGER.debug("Error checking playlist readiness: %s", err)
            await asyncio.sleep(0.5)
        return False

    async def _ensure_hls_ready(self, video_path: Path, wait_seconds: float = 2.0) -> bool:
        """Ensure HLS playlist is usable (has segments) for the given video.

        Starts packager if needed and waits for index.m3u8 to have EXTINF entries
        and at least one non-empty segment file.
        """
        hls_dir = self._get_hls_dir()
        if not hls_dir:
            return False

        playlist_path = hls_dir / "index.m3u8"

        # Start packager if not already on this video
        if self._hls_current_video is None or self._hls_current_video != video_path:
            # Start new HLS (old processes keep running)
            await self._start_hls_live(video_path)
            self._hls_current_video = video_path
            # Wait for new HLS to initialize
            await asyncio.sleep(1.0)

        # Wait for playlist to become valid
        start_time = time.time()
        ready_playlist = False
        ready_segment = False

        while time.time() - start_time < wait_seconds:
            if playlist_path.exists() and playlist_path.stat().st_size > 64:
                try:
                    async with aiofiles.open(playlist_path, "r") as f:
                        content = await f.read()
                except Exception as err:
                    _LOGGER.debug("Failed to read playlist while waiting: %s", err)
                    content = ""

                ready_playlist = "#EXTINF" in content

                # Ensure at least one segment exists and is non-empty
                try:
                    segments = list(hls_dir.glob("segment_*.ts"))
                    ready_segment = any(seg.exists() and seg.stat().st_size > 0 for seg in segments)
                except Exception as err:
                    _LOGGER.debug("Failed to inspect HLS segments: %s", err)
                    ready_segment = False

                if ready_playlist and ready_segment:
                    return True

            await asyncio.sleep(0.2)

        if not ready_playlist or not ready_segment:
            _LOGGER.warning(
                "HLS not ready: playlist_ok=%s segment_ok=%s", ready_playlist, ready_segment
            )
        return ready_playlist and ready_segment

    async def _download_loop(self) -> None:
        """Continuously download videos every 10 seconds while being viewed."""
        last_url_fetch = 0
        last_video_url = None
        fetch_interval_success = 30
        fetch_interval_fail = 3
        current_interval = fetch_interval_success
        try:
            while True:
                try:
                    current_time = time.time()
                    
                    # Stop if idle
                    if current_time - self._last_access_time > self._idle_timeout:
                        _LOGGER.info("Camera %s idle, stopping refresh", self._attr_name)
                        break
                    
                    # Fetch new URL with adaptive interval
                    if current_time - last_url_fetch > current_interval:
                        new_url = await self._fetch_video_url()
                        if new_url:
                            self._video_url = new_url
                            current_interval = fetch_interval_success
                        else:
                            current_interval = fetch_interval_fail
                            _LOGGER.warning("No video source found; retrying in %ds", current_interval)
                        last_url_fetch = current_time
                    
                    # Download and save only if URL changed or no video exists yet
                    should_download = False
                    if self._video_url:
                        if self._video_url != last_video_url:
                            _LOGGER.info("New video URL detected: %s", self._video_url)
                            should_download = True
                        elif self._hls_current_video is None:
                            _LOGGER.info("Initial download for HLS setup")
                            should_download = True
                    
                    if should_download:
                        download_success = await self._download_and_save_video()
                        if download_success:
                            last_video_url = self._video_url
                            # Start new HLS immediately (will mark ready_time when playlist is valid)
                            latest = self._get_latest_video_fresh()
                            if latest and latest.name in self._video_ready_time:
                                if self._hls_current_video is None or self._hls_current_video != latest:
                                    _LOGGER.info("Starting HLS for new video: %s", latest.name)
                                    hls_id = await self._start_hls_live(latest)
                                    self._hls_current_video = latest
                                    self._serving_video = latest
                    
                    # Check if latest HLS entry is ready - if so, mark old entries as switched
                    if self._hls_entries:
                        latest_id = max(self._hls_entries.keys())
                        latest_entry = self._hls_entries[latest_id]
                        
                        # Always update fileSize for the active entry (shows live growth)
                        hls_dir = self._get_hls_dir()
                        if hls_dir:
                            playlist_filename = latest_entry.get("playlist")
                            if playlist_filename:
                                playlist_path = hls_dir / playlist_filename
                                if playlist_path.exists():
                                    file_size = playlist_path.stat().st_size
                                    latest_entry["fileSize"] = file_size
                                    
                                    # Mark as ready when size > 1000 and has valid content
                                    if latest_entry["ready_time"] is None and file_size > 1000:
                                        try:
                                            async with aiofiles.open(playlist_path, 'r') as f:
                                                content = await f.read()
                                            if "#EXTINF" in content:
                                                latest_entry["ready_time"] = time.time()
                                                _LOGGER.info("HLS entry %d marked ready in download loop (size=%d)", latest_id, file_size)
                                        except Exception as e:
                                            _LOGGER.debug("Error reading playlist in download loop: %s", e)
                        
                        # Switch if ready and not yet switched
                        if latest_entry["ready_time"] is not None and latest_entry["switch_time"] is None:
                            _LOGGER.info("HLS entry %d is ready, marking for active use", latest_id)
                            # Mark old entries as switched (but don't kill processes - let them finish building)
                            for old_id in self._hls_entries:
                                if old_id != latest_id and self._hls_entries[old_id]["switch_time"] is None:
                                    old_entry = self._hls_entries[old_id]
                                    old_entry["switch_time"] = time.time()
                                    _LOGGER.info("HLS entry %d switched away, letting process finish", old_id)
                    
                    # Periodically clean up old HLS entries and orphan videos
                    await self._cleanup_old_hls_entries()
                    self._cleanup_videos_not_in_hls()
                    
                    # Wait before next iteration
                    sleep_time = 10
                    await asyncio.sleep(sleep_time)
                    
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    _LOGGER.error("Error in download loop: %s", e)
                    await asyncio.sleep(20)
        except asyncio.CancelledError:
            pass
        finally:
            self._refresh_task = None
            _LOGGER.debug("Refresh task stopped for %s", self._attr_name)

    @property
    def available(self) -> bool:
        """Return if camera is available."""
        return True

    @property
    def extra_state_attributes(self):
        """Return extra state attributes."""
        mjpeg_external_url = getattr(self, "_mjpeg_external_url", DEFAULT_MJPEG_EXTERNAL_URL)
        mjpeg_url = f"{mjpeg_external_url}/mjpeg/{self.get_camera_id()}"
        attrs = {
            "cctv_url": self._cctv_url,
            "camera_id": self.get_camera_id(),
            "mjpeg_url": mjpeg_url,
            "hls_url": f"{mjpeg_external_url}/api/daejeon_cctv/{self.get_camera_id()}/hls/index.m3u8",
            "mjpeg_host": getattr(self, "_mjpeg_host", DEFAULT_MJPEG_HOST),
            "mjpeg_port": getattr(self, "_mjpeg_port", DEFAULT_MJPEG_PORT),
            "mjpeg_external_url": mjpeg_external_url,
        }
        if self._video_url:
            attrs["video_url"] = self._video_url
        if self._fetch_error:
            attrs["error"] = self._fetch_error
        return attrs

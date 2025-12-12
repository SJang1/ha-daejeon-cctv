"""Camera platform for Daejeon CCTV integration."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import ssl
import time
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp
from aiohttp import web

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .const import (
    CONF_CCTV_NAME,
    CONF_CCTV_URL,
    CONF_HLS_SEGMENT_DURATION,
    CONF_MAX_SEGMENTS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HLS_SEGMENT_DURATION,
    DEFAULT_MAX_SEGMENTS,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    DOWNLOAD_RETRY_DELAY,
    FETCH_INTERVAL_FAIL,
    IDLE_TIMEOUT,
    VIDEO_BASE_DIR,
)

_LOGGER = logging.getLogger(__name__)

# SSL context for HTTPS requests
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

# Global camera registry for HTTP views
_CAMERAS: dict[str, "DaejeonCCTVCamera"] = {}


class HLSStreamManager:
    """Manages a single continuous HLS stream with seamless video switching.
    
    Uses a single HLS output directory and seamlessly switches source videos
    by restarting ffmpeg with append_list flag to continue the stream.
    """
    
    def __init__(
        self,
        camera_id: str,
        base_dir: Path,
        segment_duration: int = DEFAULT_HLS_SEGMENT_DURATION,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
    ) -> None:
        """Initialize the HLS stream manager."""
        self._camera_id = camera_id
        self._base_dir = base_dir / camera_id
        self._segment_duration = segment_duration
        self._max_segments = max_segments
        self._process: asyncio.subprocess.Process | None = None
        self._current_video: Path | None = None
        self._segment_counter = 0
        self._ready = False
        self._lock = asyncio.Lock()
        
    @property
    def hls_dir(self) -> Path:
        """Return the HLS output directory."""
        return self._base_dir / "hls"
    
    @property
    def playlist_path(self) -> Path:
        """Return the main playlist file path."""
        return self.hls_dir / "index.m3u8"
    
    @property
    def is_running(self) -> bool:
        """Check if HLS stream is running."""
        return self._process is not None and self._process.returncode is None
    
    @property
    def is_ready(self) -> bool:
        """Check if HLS stream is ready for playback."""
        return self._ready and self.is_running
    
    @property
    def current_video(self) -> Path | None:
        """Return the current video being streamed."""
        return self._current_video
    
    async def start_or_switch(self, video_path: Path) -> bool:
        """Start HLS stream or switch to a new video seamlessly.
        
        If the stream is already running with the same video, do nothing.
        If running with different video, use seamless handoff:
        1. Start new FFmpeg with append_list (appends to existing playlist)
        2. Wait for new process to produce segments
        3. Then stop the old process
        This ensures no gap in the HLS stream.
        """
        async with self._lock:
            # Same video, already running - nothing to do
            if self._current_video == video_path and self.is_running:
                return True
            
            # Ensure HLS directory exists (non-blocking)
            await asyncio.to_thread(self.hls_dir.mkdir, parents=True, exist_ok=True)
            
            # Determine if this is a fresh start or a switch
            is_switch = self._current_video is not None and self.is_running
            old_process = self._process if is_switch else None
            
            # For fresh start, clean the directory
            if not is_switch:
                try:
                    if await asyncio.to_thread(self.hls_dir.exists):
                        await asyncio.to_thread(shutil.rmtree, self.hls_dir)
                        await asyncio.to_thread(self.hls_dir.mkdir, parents=True, exist_ok=True)
                    self._segment_counter = 0
                except Exception as err:
                    _LOGGER.warning("Failed to clean HLS dir: %s", err)
            
            # Build ffmpeg command
            segment_pattern = str(self.hls_dir / "chunk_%05d.ts")
            
            hls_flags = "delete_segments+omit_endlist+program_date_time"
            if is_switch:
                hls_flags += "+append_list"
                self._segment_counter += 100  # Jump segment numbers to avoid conflicts
            
            # Create a concat file for seamless looping
            concat_file = self.hls_dir / f"loop_{self._segment_counter}.txt"
            loop_entries = "\n".join([f"file '{video_path}'" for _ in range(10000)])
            async with aiofiles.open(concat_file, "w") as f:
                await f.write(loop_entries)
            
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-f", "concat",
                "-safe", "0",
                "-re",  # Real-time processing
                "-i", str(concat_file),
                "-c:v", "copy",
                "-c:a", "copy",
                "-hls_time", str(self._segment_duration),
                "-hls_list_size", str(self._max_segments),
                "-hls_segment_type", "mpegts",
                "-hls_flags", hls_flags,
                "-start_number", str(self._segment_counter),
                "-hls_segment_filename", segment_pattern,
                "-f", "hls",
                str(self.playlist_path),
            ]
            
            try:
                new_process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                
                action = "Switching" if is_switch else "Starting"
                _LOGGER.info(
                    "%s HLS stream for camera %s (video: %s, pid: %s)",
                    action, self._camera_id, video_path.name, new_process.pid
                )
                
                # Wait for new process to start producing segments
                await asyncio.sleep(0.5)
                if new_process.returncode is not None:
                    stderr = await new_process.stderr.read() if new_process.stderr else b""
                    _LOGGER.error("New HLS process died: %s", stderr.decode(errors="ignore"))
                    # Keep old process running if switch failed
                    return False
                
                # New process is running, now safely stop old process
                if old_process:
                    try:
                        old_process.terminate()
                        await asyncio.wait_for(old_process.wait(), timeout=2.0)
                        _LOGGER.debug("Stopped old FFmpeg process")
                    except Exception:
                        try:
                            old_process.kill()
                        except Exception:
                            pass
                
                # Update state
                self._process = new_process
                self._current_video = video_path
                
                # For switches, keep ready state. For fresh start, need to check.
                if not is_switch:
                    self._ready = False
                
                return True
                
            except Exception as err:
                _LOGGER.error("Failed to start HLS stream: %s", err)
                return False
    
    async def check_ready(self) -> bool:
        """Check if HLS playlist is ready for playback."""
        if self._ready:
            return True
        
        try:
            # Non-blocking file checks
            exists = await asyncio.to_thread(self.playlist_path.exists)
            if not exists:
                return False
            
            size = await asyncio.to_thread(lambda: self.playlist_path.stat().st_size)
            if size < 100:
                return False
            
            async with aiofiles.open(self.playlist_path, "r") as f:
                content = await f.read()
            
            if "#EXTINF" in content:
                self._ready = True
                _LOGGER.info("HLS stream ready for camera %s", self._camera_id)
                return True
                
        except Exception as err:
            _LOGGER.debug("Error checking HLS readiness: %s", err)
        
        return False
    
    async def restart_if_dead(self) -> bool:
        """Restart HLS process if it died."""
        if self.is_running:
            return True
        
        if self._process and self._process.returncode is not None:
            # Process died, try to get error output
            try:
                if self._process.stderr:
                    stderr = await asyncio.wait_for(self._process.stderr.read(), timeout=0.5)
                    if stderr:
                        _LOGGER.warning("FFmpeg stderr: %s", stderr.decode(errors="ignore")[:500])
            except Exception:
                pass
        
        if self._current_video:
            video_exists = await asyncio.to_thread(self._current_video.exists)
            if video_exists:
                _LOGGER.warning("HLS process died, restarting for camera %s", self._camera_id)
                # Force a fresh start
                old_video = self._current_video
                self._current_video = None
                return await self.start_or_switch(old_video)
        
        return False
    
    async def stop(self) -> None:
        """Stop HLS streaming and clean up."""
        async with self._lock:
            if self._process:
                try:
                    self._process.terminate()
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
                except Exception:
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                self._process = None
            
            self._current_video = None
            self._ready = False
            
            # Clean up HLS directory (non-blocking)
            try:
                exists = await asyncio.to_thread(self.hls_dir.exists)
                if exists:
                    await asyncio.to_thread(shutil.rmtree, self.hls_dir)
            except Exception as err:
                _LOGGER.debug("Failed to remove HLS dir: %s", err)





async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Daejeon CCTV camera platform."""
    data = config_entry.data
    options = config_entry.options
    
    camera = DaejeonCCTVCamera(
        hass=hass,
        cctv_url=data[CONF_CCTV_URL],
        cctv_name=data[CONF_CCTV_NAME],
        segment_duration=options.get(
            CONF_HLS_SEGMENT_DURATION,
            data.get(CONF_HLS_SEGMENT_DURATION, DEFAULT_HLS_SEGMENT_DURATION)
        ),
        max_segments=options.get(
            CONF_MAX_SEGMENTS,
            data.get(CONF_MAX_SEGMENTS, DEFAULT_MAX_SEGMENTS)
        ),
        update_interval=options.get(
            CONF_UPDATE_INTERVAL,
            data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        ),
    )
    
    # Register camera globally
    camera_id = camera.camera_id
    _CAMERAS[camera_id] = camera
    _LOGGER.info("Registered camera %s with ID: %s", camera.name, camera_id)
    
    # Register HTTP views once
    domain_data = hass.data.get(DOMAIN, {})
    if not domain_data.get("views_registered"):
        hass.http.register_view(HLSPlaylistView())
        hass.http.register_view(HLSSegmentView())
        hass.data.setdefault(DOMAIN, {})["views_registered"] = True
        _LOGGER.debug("Registered HLS HTTP views")
    
    async_add_entities([camera], True)


class HLSPlaylistView(HomeAssistantView):
    """Serve HLS playlist files."""
    
    url = "/api/daejeon_cctv/{camera_id}/hls/index.m3u8"
    name = "api:daejeon_cctv:hls_playlist"
    requires_auth = False
    
    async def get(self, request: web.Request, camera_id: str) -> web.Response:
        """Handle GET request for HLS playlist."""
        camera = _CAMERAS.get(camera_id)
        if not camera:
            return web.Response(status=404, text="Camera not found")
        
        # Update access time
        camera.update_access_time()
        
        # Check if there's a newer video available and switch to it
        await camera.switch_to_latest_if_needed()
        
        hls_manager = camera.hls_manager
        if not hls_manager.is_ready:
            return web.Response(status=503, text="HLS stream not ready")
        
        try:
            async with aiofiles.open(hls_manager.playlist_path, "r") as f:
                content = await f.read()
            
            if "#EXTINF" not in content:
                return web.Response(status=503, text="Playlist incomplete")
            
            headers = {
                "Content-Type": "application/vnd.apple.mpegurl",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            }
            
            return web.Response(text=content, headers=headers)
            
        except FileNotFoundError:
            return web.Response(status=503, text="Playlist not ready")
        except Exception as err:
            _LOGGER.error("Error serving playlist: %s", err)
            return web.Response(status=500, text=str(err))


class HLSSegmentView(HomeAssistantView):
    """Serve HLS segment files."""
    
    url = "/api/daejeon_cctv/{camera_id}/hls/{filename}"
    name = "api:daejeon_cctv:hls_segment"
    requires_auth = False
    
    async def get(
        self, request: web.Request, camera_id: str, filename: str
    ) -> web.Response:
        """Handle GET request for HLS segment."""
        camera = _CAMERAS.get(camera_id)
        if not camera:
            return web.Response(status=404, text="Camera not found")
        
        camera.update_access_time()
        
        if not filename.endswith(".ts"):
            return web.Response(status=404, text="Invalid segment")
        
        file_path = camera.hls_manager.hls_dir / filename
        
        try:
            # Use aiofiles to read segment data
            async with aiofiles.open(file_path, "rb") as f:
                data = await f.read()
            
            headers = {
                "Content-Type": "video/mp2t",
                "Cache-Control": "no-cache",
            }
            
            return web.Response(body=data, headers=headers)
            
        except FileNotFoundError:
            return web.Response(status=404, text="Segment not found")
        except Exception as err:
            _LOGGER.debug("Error serving segment %s: %s", filename, err)
            return web.Response(status=404, text="Segment not found")


class DaejeonCCTVCamera(Camera):
    """Representation of a Daejeon CCTV Camera."""
    
    _attr_supported_features = CameraEntityFeature.STREAM
    
    def __init__(
        self,
        hass: HomeAssistant,
        cctv_url: str,
        cctv_name: str,
        segment_duration: int = DEFAULT_HLS_SEGMENT_DURATION,
        max_segments: int = DEFAULT_MAX_SEGMENTS,
        update_interval: int = DEFAULT_UPDATE_INTERVAL,
    ) -> None:
        """Initialize the camera."""
        super().__init__()
        
        self._hass = hass
        self._cctv_url = cctv_url
        self._attr_name = cctv_name
        self._camera_id = hashlib.md5(cctv_url.encode()).hexdigest()[:8]
        self._attr_unique_id = f"daejeon_cctv_{self._camera_id}"
        self._attr_brand = "Daejeon City"
        self._attr_model = "UTIC CCTV"
        self._attr_is_on = True
        self._update_interval = update_interval
        
        # Video management
        self._video_dir = Path(VIDEO_BASE_DIR) / self._camera_id
        self._video_url: str | None = None
        self._last_downloaded_url: str | None = None  # Track to avoid re-downloading
        self._session = async_get_clientsession(hass)
        self._video_ready: dict[str, float] = {}  # filename -> ready_time
        self._max_videos = 20
        
        # Stream manager (HLS-based, no external RTSP server needed)
        self._hls_manager = HLSStreamManager(
            camera_id=self._camera_id,
            base_dir=Path(VIDEO_BASE_DIR),
            segment_duration=segment_duration,
            max_segments=max_segments,
        )
        
        # Background task management
        self._download_task: asyncio.Task | None = None
        self._last_access_time: float = 0
        self._fetch_error: str | None = None
        
        # Snapshot cache
        self._snapshot_cache: bytes | None = None
        self._snapshot_cache_video: Path | None = None
        self._snapshot_cache_time: float = 0
    
    @property
    def camera_id(self) -> str:
        """Return the camera ID."""
        return self._camera_id
    
    @property
    def hls_manager(self) -> HLSStreamManager:
        """Return the HLS stream manager."""
        return self._hls_manager
    
    def update_access_time(self) -> None:
        """Update the last access time and ensure download task is running."""
        self._last_access_time = time.time()
        self._start_download_task()
    
    async def switch_to_latest_if_needed(self) -> bool:
        """Switch to latest video if a newer one is available.
        
        Called by HLS playlist view to check for updates.
        Returns True if switched, False if no switch needed.
        """
        latest = self._get_latest_ready_video()
        if not latest:
            return False
        
        current = self._hls_manager.current_video
        if current == latest:
            return False
        
        # There's a newer video available, switch to it
        _LOGGER.info("Switching to newer video: %s", latest.name)
        return await self._hls_manager.start_or_switch(latest)
    
    @property
    def use_stream_for_stills(self) -> bool:
        """Use snapshot generation instead of stream for stills."""
        return False
    
    def camera_image(self, width: int = 640, height: int = 480) -> bytes | None:
        """Sync fallback - unused, HA calls async version."""
        return None
    
    async def async_camera_image(
        self, width: int = 640, height: int = 480
    ) -> bytes | None:
        """Return a JPEG snapshot from the latest video."""
        self.update_access_time()
        
        latest = self._get_latest_ready_video()
        if not latest:
            _LOGGER.debug("No video ready for snapshot")
            return None
        
        # Check cache
        now = time.time()
        if (
            self._snapshot_cache_video == latest
            and (now - self._snapshot_cache_time) < 2
            and self._snapshot_cache
        ):
            return self._snapshot_cache
        
        # Generate snapshot with ffmpeg
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-ss", "0",
            "-i", str(latest),
            "-vframes", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
        ]
        
        if width and width > 0:
            cmd.extend(["-vf", f"scale='min({width},iw)':-1"])
        
        cmd.append("-")
        
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
            
            if proc.returncode != 0 or not stdout:
                _LOGGER.warning("Snapshot failed: %s", stderr.decode(errors="ignore"))
                return None
            
            # Cache snapshot
            self._snapshot_cache = stdout
            self._snapshot_cache_video = latest
            self._snapshot_cache_time = time.time()
            
            return stdout
            
        except asyncio.TimeoutError:
            _LOGGER.warning("Snapshot generation timed out")
            return None
        except Exception as err:
            _LOGGER.error("Snapshot error: %s", err)
            return None
    
    async def async_get_stream_source(self) -> str | None:
        """Return HLS stream URL for Home Assistant stream integration.
        
        The HLS stream loops infinitely until a new video segment arrives.
        """
        self.update_access_time()
        
        # Ensure we have a video and HLS is running
        latest = self._get_latest_ready_video()
        if not latest:
            _LOGGER.warning("No video ready for stream: %s", self._attr_name)
            return None
        
        # Start or switch HLS stream
        await self._hls_manager.start_or_switch(latest)
        
        # Wait briefly for HLS to be ready
        for _ in range(10):
            if await self._hls_manager.check_ready():
                break
            await asyncio.sleep(0.5)
        
        if not self._hls_manager.is_ready:
            _LOGGER.warning("HLS stream not ready for: %s", self._attr_name)
            # Fall back to direct file (won't loop but at least plays)
            return str(latest)
        
        # Build full HTTP URL for HLS stream
        # HA's stream integration needs a full URL, not a relative path
        try:
            base_url = get_url(self._hass, prefer_external=False, allow_internal=True)
            hls_url = f"{base_url}/api/daejeon_cctv/{self._camera_id}/hls/index.m3u8"
            _LOGGER.info("Stream source (HLS): %s", hls_url)
            return hls_url
        except Exception as err:
            _LOGGER.warning("Could not get HA URL, falling back to file: %s", err)
            return str(latest)
    
    async def stream_source(self) -> str | None:
        """Compatibility wrapper for stream source."""
        return await self.async_get_stream_source()
    
    async def async_added_to_hass(self) -> None:
        """Called when entity is added to hass."""
        await super().async_added_to_hass()
        # Delay start to avoid blocking HA startup
        # Download will start when stream is first accessed or after delay
        self._hass.loop.call_later(5, self._delayed_start)
    
    def _delayed_start(self) -> None:
        """Start download task after delay (called by loop.call_later)."""
        self._last_access_time = time.time()
        self._start_download_task()
    
    async def async_will_remove_from_hass(self) -> None:
        """Called when entity is being removed."""
        await super().async_will_remove_from_hass()
        
        # Stop all processes
        self._stop_download_task()
        await self._hls_manager.stop()
        
        # Unregister camera
        _CAMERAS.pop(self._camera_id, None)
    
    def _start_download_task(self) -> None:
        """Start the download task if not running."""
        if self._download_task is None or self._download_task.done():
            self._download_task = self._hass.loop.create_task(self._download_loop())
            _LOGGER.debug("Started download task for %s", self._attr_name)
    
    def _stop_download_task(self) -> None:
        """Stop the download task."""
        if self._download_task and not self._download_task.done():
            self._download_task.cancel()
            self._download_task = None
            _LOGGER.debug("Stopped download task for %s", self._attr_name)
    
    def _get_latest_ready_video(self) -> Path | None:
        """Get the newest ready video file (uses cached info, no blocking I/O)."""
        if not self._video_ready:
            return None
        
        # Use cached ready times instead of filesystem stat
        # Sort by ready time (most recent first)
        sorted_ready = sorted(
            self._video_ready.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        
        for filename, _ in sorted_ready:
            video_path = self._video_dir / filename
            # Quick check using cached info - the file should exist if in ready dict
            if filename in self._video_ready:
                return video_path
        
        return None
    
    async def _fetch_video_url(self) -> str | None:
        """Fetch video URL from CCTV page."""
        try:
            url = self._cctv_url
            if not url.startswith(("http://", "https://")):
                url = f"https://{url}"
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
            }
            
            connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, headers=headers, timeout=15) as response:
                    if response.status != 200:
                        _LOGGER.error("Failed to fetch CCTV page: %s", response.status)
                        return None
                    
                    html = await response.text()
                    
                    # Look for .mp4 URLs
                    patterns = [
                        r'https://tportal\.daejeon\.go\.kr[^\s"\'<>]*\.mp4',
                        r'https://[^\s"\'<>]*\.mp4',
                    ]
                    
                    for pattern in patterns:
                        urls = re.findall(pattern, html)
                        if urls:
                            video_url = urls[0]
                            _LOGGER.info("Found video URL: %s", video_url)
                            return video_url
                    
                    _LOGGER.debug("No video URL found in page")
                    return None
                    
        except Exception as err:
            _LOGGER.error("Error fetching video URL: %s", err)
            return None
    
    async def _download_video(self) -> bool:
        """Download video from source URL."""
        if not self._video_url:
            return False
        
        try:
            # Use asyncio.to_thread for blocking mkdir
            await asyncio.to_thread(self._video_dir.mkdir, parents=True, exist_ok=True)
            
            async with self._session.get(self._video_url, timeout=30) as response:
                if response.status != 200:
                    _LOGGER.error("Download failed: %s", response.status)
                    return False
                
                timestamp = time.strftime("%Y%m%d_%H%M%S")
                file_path = self._video_dir / f"video_{timestamp}.mp4"
                
                async with aiofiles.open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(65536):
                        await f.write(chunk)
                
                # Mark as ready
                self._video_ready[file_path.name] = time.time()
                _LOGGER.info("Downloaded video: %s", file_path.name)
                
                # Clean up old videos (run in background to avoid blocking)
                asyncio.create_task(self._cleanup_old_videos())
                self._fetch_error = None
                
                return True
                
        except Exception as err:
            self._fetch_error = str(err)
            _LOGGER.error("Download error: %s", err)
            return False
    
    async def _cleanup_old_videos(self) -> None:
        """Remove old video files (async to avoid blocking)."""
        try:
            await asyncio.to_thread(self._cleanup_old_videos_sync)
        except Exception as err:
            _LOGGER.debug("Cleanup error: %s", err)
    
    def _cleanup_old_videos_sync(self) -> None:
        """Remove old video files (sync version for thread)."""
        if not self._video_dir.exists():
            return
        
        videos = sorted(
            self._video_dir.glob("video_*.mp4"),
            key=lambda p: p.stat().st_mtime if p.exists() else 0,
        )
        
        if len(videos) <= self._max_videos:
            return
        
        # Keep current video being streamed by HLS
        keep_videos: set[Path] = set()
        if self._hls_manager.current_video:
            keep_videos.add(self._hls_manager.current_video)
        
        # Delete old videos
        for video in videos[:-self._max_videos]:
            if video in keep_videos:
                continue
            
            # Don't delete very recent videos
            if video.name in self._video_ready:
                age = time.time() - self._video_ready[video.name]
                if age < 30:
                    continue
            
            try:
                video.unlink()
                self._video_ready.pop(video.name, None)
                _LOGGER.debug("Deleted old video: %s", video.name)
            except OSError:
                pass
    
    async def _download_loop(self) -> None:
        """Main download loop - fetches videos in background.
        
        Downloads new videos but does NOT switch the HLS stream automatically.
        The stream will switch to the new video only when the viewer refreshes.
        This avoids timestamp discontinuity issues.
        """
        last_url_fetch: float = 0
        current_interval = self._update_interval
        
        try:
            while True:
                current_time = time.time()
                
                # Stop if idle
                if current_time - self._last_access_time > IDLE_TIMEOUT:
                    _LOGGER.info("Camera %s idle, stopping", self._attr_name)
                    break
                
                # Fetch video URL periodically
                if current_time - last_url_fetch > current_interval:
                    new_url = await self._fetch_video_url()
                    
                    if new_url:
                        self._video_url = new_url
                        current_interval = self._update_interval
                        
                        # Only download if URL changed (new video segment from source)
                        if new_url != self._last_downloaded_url:
                            _LOGGER.debug("New video URL detected, downloading...")
                            # Retry download up to 3 times
                            download_success = False
                            for attempt in range(3):
                                if await self._download_video():
                                    download_success = True
                                    self._last_downloaded_url = new_url
                                    _LOGGER.info("New video ready, will use on next stream request")
                                    break
                                else:
                                    _LOGGER.warning(
                                        "Download failed (attempt %d/3), retrying in %ds",
                                        attempt + 1, DOWNLOAD_RETRY_DELAY
                                    )
                                    await asyncio.sleep(DOWNLOAD_RETRY_DELAY)
                            
                            if not download_success:
                                _LOGGER.error("Download failed after 3 attempts")
                        
                        # Ensure HLS is running (but don't switch videos)
                        if not self._hls_manager.is_running:
                            latest = self._get_latest_ready_video()
                            if latest:
                                _LOGGER.debug("HLS not running, starting...")
                                await self._hls_manager.start_or_switch(latest)
                    else:
                        current_interval = FETCH_INTERVAL_FAIL
                        _LOGGER.debug("No video found, retry in %ds", current_interval)
                    
                    last_url_fetch = current_time
                
                # Monitor HLS health and restart if dead
                await self._hls_manager.restart_if_dead()
                
                await asyncio.sleep(1.0)
                
        except asyncio.CancelledError:
            pass
        except Exception as err:
            _LOGGER.error("Download loop error: %s", err)
        finally:
            self._download_task = None
    
    @property
    def available(self) -> bool:
        """Return True if camera is available."""
        return True
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {
            "cctv_url": self._cctv_url,
            "camera_id": self._camera_id,
            "hls_url": f"/api/daejeon_cctv/{self._camera_id}/hls/index.m3u8",
        }
        
        if self._video_url:
            attrs["video_url"] = self._video_url
        if self._fetch_error:
            attrs["error"] = self._fetch_error
        
        return attrs

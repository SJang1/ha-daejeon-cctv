"""Config flow for Daejeon CCTV integration."""
from __future__ import annotations

import hashlib
import logging
import re
import ssl
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CCTV_NAME,
    CONF_CCTV_URL,
    CONF_HLS_SEGMENT_DURATION,
    CONF_MAX_SEGMENTS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_HLS_SEGMENT_DURATION,
    DEFAULT_MAX_SEGMENTS,
    DEFAULT_NAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# SSL context for validation
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CCTV_URL): cv.string,
        vol.Optional(CONF_CCTV_NAME): cv.string,
        vol.Optional(
            CONF_HLS_SEGMENT_DURATION, default=DEFAULT_HLS_SEGMENT_DURATION
        ): cv.positive_int,
        vol.Optional(CONF_MAX_SEGMENTS, default=DEFAULT_MAX_SEGMENTS): cv.positive_int,
    }
)


def extract_cctv_name(url: str) -> str:
    """Extract and decode the CCTV name from the URL."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if "cctvname" in params:
            name = unquote(params["cctvname"][0])
            return name
        return DEFAULT_NAME
    except Exception as e:
        _LOGGER.error("Failed to extract CCTV name from URL: %s", e)
        return DEFAULT_NAME


async def validate_cctv_url(url: str) -> tuple[bool, str | None]:
    """Validate that the CCTV URL is accessible and contains video."""
    try:
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        }
        
        connector = aiohttp.TCPConnector(ssl=_SSL_CONTEXT)
        timeout = aiohttp.ClientTimeout(total=5)  # Short timeout to avoid blocking
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    return False, f"HTTP {response.status}"
                
                html = await response.text()
                
                # Check for video URL patterns
                patterns = [
                    r'https://tportal\.daejeon\.go\.kr[^\s"\'<>]*\.mp4',
                    r'https://[^\s"\'<>]*\.mp4',
                ]
                
                for pattern in patterns:
                    if re.search(pattern, html):
                        return True, None
                
                return False, "no_video_found"
                
    except aiohttp.ClientError as err:
        return False, f"connection_error: {err}"
    except Exception as err:
        return False, f"unknown_error: {err}"


class DaejeonCCTVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Handle a config flow for Daejeon CCTV."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            cctv_url = user_input[CONF_CCTV_URL].strip()
            
            # Validate URL (but don't block on failure - server is unreliable)
            valid, error = await validate_cctv_url(cctv_url)
            
            if not valid:
                # Log warning but proceed anyway - server may be temporarily unavailable
                _LOGGER.warning(
                    "CCTV URL validation failed (%s), but proceeding anyway", error
                )
            
            # Extract or use provided name
            cctv_name = user_input.get(CONF_CCTV_NAME)
            if not cctv_name:
                cctv_name = extract_cctv_name(cctv_url)
            
            # Generate unique ID
            url_hash = hashlib.md5(cctv_url.encode()).hexdigest()[:8]
            await self.async_set_unique_id(f"daejeon_cctv_{url_hash}")
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=cctv_name,
                data={
                    CONF_CCTV_URL: cctv_url,
                    CONF_CCTV_NAME: cctv_name,
                    CONF_HLS_SEGMENT_DURATION: user_input.get(
                        CONF_HLS_SEGMENT_DURATION, DEFAULT_HLS_SEGMENT_DURATION
                    ),
                    CONF_MAX_SEGMENTS: user_input.get(
                        CONF_MAX_SEGMENTS, DEFAULT_MAX_SEGMENTS
                    ),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Create the options flow."""
        return DaejeonCCTVOptionsFlow()


class DaejeonCCTVOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Daejeon CCTV."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_UPDATE_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_UPDATE_INTERVAL,
                            self.config_entry.data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
                        ),
                    ): cv.positive_int,
                    vol.Optional(
                        CONF_HLS_SEGMENT_DURATION,
                        default=self.config_entry.options.get(
                            CONF_HLS_SEGMENT_DURATION,
                            self.config_entry.data.get(CONF_HLS_SEGMENT_DURATION, DEFAULT_HLS_SEGMENT_DURATION)
                        ),
                    ): cv.positive_int,
                    vol.Optional(
                        CONF_MAX_SEGMENTS,
                        default=self.config_entry.options.get(
                            CONF_MAX_SEGMENTS,
                            self.config_entry.data.get(CONF_MAX_SEGMENTS, DEFAULT_MAX_SEGMENTS)
                        ),
                    ): cv.positive_int,
                }
            ),
        )

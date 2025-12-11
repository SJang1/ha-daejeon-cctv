"""Config flow for Daejeon CCTV integration."""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CCTV_NAME,
    CONF_CCTV_URL,
    CONF_MJPEG_HOST,
    CONF_MJPEG_PORT,
    CONF_MJPEG_EXTERNAL_URL,
    DEFAULT_NAME,
    DEFAULT_MJPEG_HOST,
    DEFAULT_MJPEG_PORT,
    DEFAULT_MJPEG_EXTERNAL_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CCTV_URL): cv.string,
        vol.Optional(CONF_MJPEG_HOST, default=DEFAULT_MJPEG_HOST): cv.string,
        vol.Optional(CONF_MJPEG_PORT, default=DEFAULT_MJPEG_PORT): cv.port,
        vol.Optional(CONF_MJPEG_EXTERNAL_URL, default=DEFAULT_MJPEG_EXTERNAL_URL): cv.string,
    }
)


def extract_cctv_name(url: str) -> str:
    """Extract and decode the CCTV name from the URL."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        if "cctvname" in params:
            # URL decode the name
            name = unquote(params["cctvname"][0])
            return name
        return DEFAULT_NAME
    except Exception as e:
        _LOGGER.error("Failed to extract CCTV name from URL: %s", e)
        return DEFAULT_NAME


class DaejeonCCTVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Daejeon CCTV."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                cctv_url = user_input[CONF_CCTV_URL]
                
                # Extract CCTV name from URL
                cctv_name = extract_cctv_name(cctv_url)
                
                # Create unique ID based on URL
                await self.async_set_unique_id(cctv_url)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=cctv_name,
                    data={
                        CONF_CCTV_URL: cctv_url,
                        CONF_CCTV_NAME: cctv_name,
                        CONF_MJPEG_HOST: user_input.get(CONF_MJPEG_HOST, DEFAULT_MJPEG_HOST),
                        CONF_MJPEG_PORT: user_input.get(CONF_MJPEG_PORT, DEFAULT_MJPEG_PORT),
                    },
                )
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

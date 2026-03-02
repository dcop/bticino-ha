"""Config flow for BTicino Classe 100X."""

from __future__ import annotations

import logging
import ssl
import os
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    CONF_SIP_URI,
    CONF_SIP_PASSWORD,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CA_CERT,
    CONF_DTMF_COMMAND,
    SIP_HOST,
    SIP_PORT,
)

_LOGGER = logging.getLogger(__name__)

STEP_SIP_SCHEMA = vol.Schema({
    vol.Required(CONF_SIP_URI):      cv.string,
    vol.Required(CONF_SIP_PASSWORD): cv.string,
    vol.Optional(CONF_DTMF_COMMAND, default="#"): cv.string,
})

STEP_CERT_SCHEMA = vol.Schema({
    vol.Required(CONF_CLIENT_CERT): cv.string,   # file path
    vol.Required(CONF_CLIENT_KEY):  cv.string,   # file path
    vol.Required(CONF_CA_CERT):     cv.string,   # file path
})


class BticinoConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for BTicino Classe 100X."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    # ── Step 1: SIP credentials ────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_certificates()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_SIP_SCHEMA,
            errors=errors,
            description_placeholders={
                "example_uri": "0b66038a-xxxx_AABBCCDDEEFF@gatewayId.bs.iotleg.com"
            },
        )

    # ── Step 2: mTLS certificates ──────────────────────────────────────────────

    async def async_step_certificates(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            config_dir = self.hass.config.config_dir

            def _resolve(p: str) -> str:
                """Accept absolute paths or paths relative to the HA config dir."""
                return p if os.path.isabs(p) else os.path.join(config_dir, p)

            cert_path = _resolve(user_input[CONF_CLIENT_CERT])
            key_path  = _resolve(user_input[CONF_CLIENT_KEY])
            ca_path   = _resolve(user_input[CONF_CA_CERT])
            _LOGGER.debug("Cert paths: %s | %s | %s", cert_path, key_path, ca_path)

            # Validate the certificate/key paths and build a valid TLS context
            cert_error = await self.hass.async_add_executor_job(
                _validate_certs, cert_path, key_path, ca_path,
            )
            if cert_error:
                errors["base"] = cert_error
            else:
                # Store resolved absolute paths
                self._data.update({
                    **user_input,
                    CONF_CLIENT_CERT: cert_path,
                    CONF_CLIENT_KEY:  key_path,
                    CONF_CA_CERT:     ca_path,
                })
                # Test actual SIP connection
                conn_ok = await _test_sip_connection(
                    self.hass, cert_path, key_path, ca_path,
                )
                if not conn_ok:
                    errors["base"] = "cannot_connect"
                else:
                    title = self._data[CONF_SIP_URI].split("@")[1].split(".")[0]
                    return self.async_create_entry(
                        title=f"BTicino {title}",
                        data=self._data,
                    )

        return self.async_show_form(
            step_id="certificates",
            data_schema=STEP_CERT_SCHEMA,
            errors=errors,
        )


# ── Validation helpers (run in executor — blocking I/O) ────────────────────────

def _validate_certs(cert_path: str, key_path: str, ca_path: str) -> str | None:
    """
    Check that the cert/key/CA files exist and form a valid TLS context.
    Returns None on success, or an error key string on failure.
    """
    try:
        for p in (cert_path, key_path, ca_path):
            if not os.path.isfile(p):
                _LOGGER.error("Certificate file not found: %s", p)
                return "file_not_found"
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_cert_chain(cert_path, key_path)
        ctx.load_verify_locations(cafile=ca_path)
        return None
    except ssl.SSLError as exc:
        _LOGGER.error("Certificate validation failed: %s", exc)
        return "invalid_cert"
    except Exception as exc:
        _LOGGER.error("Unexpected cert error: %s", exc)
        return "invalid_cert"


async def _test_sip_connection(
    hass: HomeAssistant, cert_path: str, key_path: str, ca_path: str
) -> bool:
    """Attempt a TLS handshake to the SIP cloud server to verify the certificate works."""

    def _blocking_test() -> bool:
        import socket as _socket
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.load_cert_chain(cert_path, key_path)
            ctx.load_verify_locations(cafile=ca_path)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_OPTIONAL

            with _socket.create_connection((SIP_HOST, SIP_PORT), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=SIP_HOST):
                    return True
        except Exception as exc:
            _LOGGER.error("SIP connection test failed: %s", exc)
            return False

    return await hass.async_add_executor_job(_blocking_test)

"""BticinoCoordinator — bridges the SIP client with Home Assistant entities."""

from __future__ import annotations

import logging
from typing import List, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_SIP_URI,
    CONF_SIP_PASSWORD,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CA_CERT,
    CONF_DTMF_COMMAND,
    EVENT_DOORBELL_RING,
    EVENT_DOORBELL_END,
)
from .sip_client import BticinoSIPClient

_LOGGER = logging.getLogger(__name__)


class BticinoCoordinator:
    """
    Owns the SIP client and exposes state to HA entities.

    Entities register themselves via add_listener() to be notified of state
    changes, avoiding the need for a polling-based DataUpdateCoordinator.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass  = hass
        self.entry = entry

        self._listeners: list = []
        self._ringing_call_id:  Optional[str] = None
        self._ringing_caller:   Optional[str] = None

        self.sip = BticinoSIPClient(
            sip_uri      = entry.data[CONF_SIP_URI],
            sip_password = entry.data[CONF_SIP_PASSWORD],
            cert_path    = entry.data[CONF_CLIENT_CERT],
            key_path     = entry.data[CONF_CLIENT_KEY],
            ca_path      = entry.data[CONF_CA_CERT],
            dtmf_command = entry.data.get(CONF_DTMF_COMMAND, "#"),
            on_call_incoming = self._on_incoming,
            on_call_ended    = self._on_ended,
        )

    # ── Public lifecycle ───────────────────────────────────────────────────────

    async def async_start(self) -> None:
        await self.sip.async_start()

    async def async_stop(self) -> None:
        await self.sip.async_stop()

    # ── State accessors ────────────────────────────────────────────────────────

    @property
    def is_registered(self) -> bool:
        return self.sip.is_registered

    @property
    def is_ringing(self) -> bool:
        return self._ringing_call_id is not None

    @property
    def ringing_caller(self) -> Optional[str]:
        return self._ringing_caller

    # ── Actions ────────────────────────────────────────────────────────────────

    async def async_open_door(self) -> bool:
        if not self._ringing_call_id:
            _LOGGER.warning("open_door called but no active doorbell call")
            return False
        result = await self.sip.async_answer_and_open(self._ringing_call_id)
        # State will be cleared when BYE arrives via on_call_ended
        return result

    async def async_reject_call(self) -> bool:
        if not self._ringing_call_id:
            return False
        await self.sip.async_reject_call(self._ringing_call_id)
        return True

    # ── Entity listener registry ───────────────────────────────────────────────

    @callback
    def add_listener(self, update_callback) -> None:
        self._listeners.append(update_callback)

    @callback
    def remove_listener(self, update_callback) -> None:
        self._listeners.discard(update_callback) if hasattr(
            self._listeners, "discard"
        ) else None
        try:
            self._listeners.remove(update_callback)
        except ValueError:
            pass

    @callback
    def _notify_listeners(self) -> None:
        for cb in self._listeners:
            try:
                cb()
            except Exception as exc:
                _LOGGER.error("Listener error: %s", exc)

    # ── SIP callbacks (called from asyncio tasks) ──────────────────────────────

    def _on_incoming(self, call_id: str, caller: str) -> None:
        """Called by SIP client when INVITE is received."""
        self._ringing_call_id = call_id
        self._ringing_caller  = caller

        # Fire HA event (for automations)
        self.hass.bus.async_fire(
            EVENT_DOORBELL_RING,
            {"call_id": call_id, "caller": caller},
        )
        self._notify_listeners()
        _LOGGER.info("Doorbell event fired: caller=%s", caller)

    def _on_ended(self, call_id: str) -> None:
        """Called by SIP client when call ends (BYE or CANCEL)."""
        if self._ringing_call_id == call_id:
            self._ringing_call_id = None
            self._ringing_caller  = None

        self.hass.bus.async_fire(EVENT_DOORBELL_END, {"call_id": call_id})
        self._notify_listeners()

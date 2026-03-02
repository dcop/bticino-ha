"""BTicino Classe 100X — button platform.

Provides two buttons:
  • Open Door   — answers the current incoming call and sends door-open DTMF
  • Reject Call — declines the current incoming call (486 Busy Here)
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import device_info
from .const import DOMAIN
from .coordinator import BticinoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BticinoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        OpenDoorButton(coordinator, entry),
        RejectCallButton(coordinator, entry),
    ])


# ── Open Door ─────────────────────────────────────────────────────────────────

class OpenDoorButton(ButtonEntity):
    """Answers the active doorbell call and sends the door-open DTMF command."""

    _attr_has_entity_name = True
    _attr_name            = "Open Door"
    _attr_icon            = "mdi:door-open"
    _attr_should_poll     = False

    def __init__(self, coordinator: BticinoCoordinator, entry: ConfigEntry) -> None:
        self._coordinator       = coordinator
        self._attr_unique_id    = f"{entry.entry_id}_open_door"
        self._attr_device_info  = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._coordinator.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.remove_listener(self._handle_update)

    @property
    def available(self) -> bool:
        """Only available when there is an active doorbell call."""
        return self._coordinator.is_ringing

    async def async_press(self) -> None:
        _LOGGER.info("Open Door pressed")
        success = await self._coordinator.async_open_door()
        if not success:
            _LOGGER.warning("Open Door failed — no active call?")

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


# ── Reject Call ───────────────────────────────────────────────────────────────

class RejectCallButton(ButtonEntity):
    """Rejects the active doorbell call (sends 486 Busy Here)."""

    _attr_has_entity_name = True
    _attr_name            = "Reject Call"
    _attr_icon            = "mdi:phone-hangup"
    _attr_should_poll     = False

    def __init__(self, coordinator: BticinoCoordinator, entry: ConfigEntry) -> None:
        self._coordinator       = coordinator
        self._attr_unique_id    = f"{entry.entry_id}_reject_call"
        self._attr_device_info  = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self._coordinator.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.remove_listener(self._handle_update)

    @property
    def available(self) -> bool:
        return self._coordinator.is_ringing

    async def async_press(self) -> None:
        _LOGGER.info("Reject Call pressed")
        await self._coordinator.async_reject_call()

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

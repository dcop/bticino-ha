"""BTicino Classe 100X — binary_sensor platform.

Exposes a single binary_sensor that is ON while the doorbell is ringing
(an incoming SIP INVITE is pending) and OFF otherwise.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import device_info
from .const import DOMAIN, DATA_COORDINATOR
from .coordinator import BticinoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BticinoCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([DoorbellBinarySensor(coordinator, entry)])


class DoorbellBinarySensor(BinarySensorEntity):
    """Binary sensor: ON when doorbell is ringing (incoming SIP INVITE pending)."""

    _attr_device_class           = BinarySensorDeviceClass.OCCUPANCY
    _attr_has_entity_name        = True
    _attr_name                   = "Doorbell"
    _attr_icon                   = "mdi:doorbell"
    _attr_should_poll            = False

    def __init__(self, coordinator: BticinoCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id    = f"{entry.entry_id}_doorbell"
        self._attr_device_info  = device_info(entry)

    # ── Entity lifecycle ───────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        self._coordinator.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._coordinator.remove_listener(self._handle_update)

    # ── State ──────────────────────────────────────────────────────────────────

    @property
    def is_on(self) -> bool:
        return self._coordinator.is_ringing

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "caller":      self._coordinator.ringing_caller,
            "registered":  self._coordinator.is_registered,
        }

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

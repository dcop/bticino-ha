"""BTicino Classe 100X — Home Assistant integration.

Sets up a persistent mTLS SIP connection to the Legrand cloud proxy and
exposes doorbell events plus door-open / reject actions as HA entities.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    PLATFORMS,
    DATA_COORDINATOR,
    EVENT_DOORBELL_RING,
    EVENT_DOORBELL_END,
    CONF_SIP_URI,
    CONF_SIP_PASSWORD,
    CONF_CLIENT_CERT,
    CONF_CLIENT_KEY,
    CONF_CA_CERT,
    CONF_DTMF_COMMAND,
)
from .coordinator import BticinoCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up BTicino Classe 100X from a config entry."""

    coordinator = BticinoCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Forward setup to platforms (button, binary_sensor)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start the SIP client (non-blocking — runs as background task)
    await coordinator.async_start()

    # Register integration-level services
    _register_services(hass)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload BTicino config entry."""
    coordinator: BticinoCoordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
    if coordinator:
        await coordinator.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok


# ── HA device info (shared across all entities) ────────────────────────────────

def device_info(entry: ConfigEntry) -> dict:
    sip_uri = entry.data.get(CONF_SIP_URI, "")
    gateway = sip_uri.split("@")[1].split(".")[0] if "@" in sip_uri else "unknown"
    return {
        "identifiers":    {(DOMAIN, entry.entry_id)},
        "name":           "BTicino Classe 100X",
        "manufacturer":   "BTicino / Legrand",
        "model":          "Classe 100X",
        "sw_version":     entry.version,
        "configuration_url": "https://door-entry.myhomeplay.com",
    }


# ── Services ──────────────────────────────────────────────────────────────────

def _register_services(hass: HomeAssistant) -> None:
    """Register integration services (idempotent)."""
    if hass.services.has_service(DOMAIN, "open_door"):
        return

    async def _svc_open_door(call: ServiceCall) -> None:
        """Service bticino_c100x.open_door — opens the door for the active call."""
        entry_id = call.data.get("entry_id")
        for eid, coordinator in hass.data.get(DOMAIN, {}).items():
            if entry_id and eid != entry_id:
                continue
            await coordinator.async_open_door()

    async def _svc_reject_call(call: ServiceCall) -> None:
        """Service bticino_c100x.reject_call — rejects the active call."""
        entry_id = call.data.get("entry_id")
        for eid, coordinator in hass.data.get(DOMAIN, {}).items():
            if entry_id and eid != entry_id:
                continue
            await coordinator.async_reject_call()

    hass.services.async_register(DOMAIN, "open_door",  _svc_open_door)
    hass.services.async_register(DOMAIN, "reject_call", _svc_reject_call)

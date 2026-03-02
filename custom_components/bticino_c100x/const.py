"""Constants for the BTicino Classe 100X integration."""

DOMAIN = "bticino_c100x"

# Config entry keys
CONF_SIP_URI      = "sip_uri"
CONF_SIP_PASSWORD = "sip_password"
CONF_CLIENT_CERT  = "client_cert"
CONF_CLIENT_KEY   = "client_key"
CONF_CA_CERT      = "ca_cert"
CONF_DTMF_COMMAND = "dtmf_command"   # default "#"

# HA event names
EVENT_DOORBELL_RING = f"{DOMAIN}_doorbell"
EVENT_DOORBELL_END  = f"{DOMAIN}_call_ended"

# hass.data keys
DATA_COORDINATOR = "coordinator"

# SIP server (Legrand cloud proxy)
SIP_HOST    = "vdesip.bs.iotleg.com"
SIP_PORT    = 5228
SIP_EXPIRES = 3600          # REGISTER expiry in seconds
SIP_RENEW   = 60            # re-register this many seconds before expiry

# Platform list
PLATFORMS = ["button", "binary_sensor"]

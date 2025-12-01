"""Constants for ECHONET Lite protocol and HEMS communication."""

# ECHONET Lite Transport
ECHONET_PORT = 3610
ECHONET_MULTICAST = "224.0.23.0"

# Node Profile
NODE_PROFILE_CLASS = 0x0EF0
NODE_PROFILE_INSTANCE = 0x0EF001

# Controller
CONTROLLER_CLASS = 0x05FF
CONTROLLER_INSTANCE = 0x05FF01

# ESV (Service Codes)
ESV_SET_SNA = 0x51  # Set response with some properties unavailable
ESV_GET_SNA = 0x52  # Get response with some properties unavailable
ESV_INF_SNA = 0x53  # Notification request SNA
ESV_SETC = 0x61  # Set with response
ESV_GET = 0x62  # Get request
ESV_INF_REQ = 0x63  # Notification request
ESV_SET_RES = 0x71  # Set response
ESV_GET_RES = 0x72  # Get response
ESV_INF = 0x73  # Notification

# EPC (Property Codes)
EPC_IDENTIFICATION_NUMBER = 0x83
EPC_MANUFACTURER_CODE = 0x8A
EPC_PRODUCT_CODE = 0x8C
EPC_SERIAL_NUMBER = 0x8D
EPC_INSTANCE_LIST = 0xD5
EPC_SELF_NODE_INSTANCE_LIST = 0xD6

# Default EPCs for node discovery (required for identification)
DISCOVERY_DEFAULT_EPCS: list[int] = [
    EPC_IDENTIFICATION_NUMBER,
    EPC_SELF_NODE_INSTANCE_LIST,
]

# Retry settings for Get requests
GET_TIMEOUT = 5.0  # Seconds to wait for response
GET_MAX_RETRIES = 3  # Maximum retry attempts for failed properties

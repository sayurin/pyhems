"""Constants for ECHONET Lite protocol and HEMS communication."""

from .eoj import EOJ

# ECHONET Lite Transport
ECHONET_PORT = 3610
ECHONET_MULTICAST = "224.0.23.0"

# Node Profile
NODE_PROFILE_INSTANCE = EOJ(0x0EF001)
NODE_PROFILE_CLASS = NODE_PROFILE_INSTANCE.class_code

# Controller
CONTROLLER_INSTANCE = EOJ(0x05FF01)
CONTROLLER_CLASS = CONTROLLER_INSTANCE.class_code

# ESV (Service Codes)
ESV_SET_SNA = 0x51  # Set response with some properties unavailable
ESV_GET_SNA = 0x52  # Get response with some properties unavailable
ESV_INF_SNA = 0x53  # Notification request response with some properties unavailable
ESV_SETC = 0x61  # Set with response
ESV_GET = 0x62  # Get request
ESV_INF_REQ = 0x63  # Notification request
ESV_SET_RES = 0x71  # Set response
ESV_GET_RES = 0x72  # Get response
ESV_INF = 0x73  # Notification
ESV_INFC = 0x74  # Notification with confirmation (INFC)
ESV_INFC_RES = 0x7A  # Confirmation response to INFC

# EPC (Property Codes)
EPC_INSTALLATION_LOCATION = 0x81
EPC_IDENTIFICATION_NUMBER = 0x83
EPC_MANUFACTURER_CODE = 0x8A
EPC_PRODUCT_CODE = 0x8C
EPC_SERIAL_NUMBER = 0x8D
EPC_INF_PROPERTY_MAP = 0x9D
EPC_SET_PROPERTY_MAP = 0x9E
EPC_GET_PROPERTY_MAP = 0x9F
EPC_INSTANCE_LIST = 0xD5
EPC_SELF_NODE_INSTANCE_LIST = 0xD6

# Default EPCs for node discovery and DeviceManager node profile fallbacks.
DISCOVERY_DEFAULT_EPCS: list[int] = [
    EPC_IDENTIFICATION_NUMBER,
    EPC_MANUFACTURER_CODE,
    EPC_PRODUCT_CODE,
    EPC_SERIAL_NUMBER,
    EPC_SELF_NODE_INSTANCE_LIST,
]

# Timeout for one-time, setup-phase requests: the initial multi-property Get
# (base_epcs + monitored_epcs) in DeviceManager.setup_device(), and the
# subsequent INF_REQ (0x63) notification-subscription request and its
# 0x73/0x53 response. Neither runs on a recurring cadence (periodic polling
# uses DeviceManager.poll_device(), a raw send() with its own, much longer
# adaptive timeout in PropertyPoller), so a generous value here does not
# affect steady-state responsiveness. Some real devices have been observed
# taking 15-20s to answer even a single-EPC request, and a too-short timeout
# on the (retried) initial Get causes duplicate, wasted load: the device
# ends up answering the same large request twice.
SETUP_REQUEST_TIMEOUT = 30.0

# Retry settings for the initial setup Get (see SETUP_REQUEST_TIMEOUT above).
GET_MAX_RETRIES = 3  # Maximum retry attempts for failed properties

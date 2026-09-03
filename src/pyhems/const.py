"""Constants for ECHONET Lite protocol and HEMS communication."""

from enum import IntEnum

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


class ESV(IntEnum):
    """ECHONET Lite service values."""

    SETI_SNA = 0x50
    SETC_SNA = 0x51
    GET_SNA = 0x52
    INF_SNA = 0x53
    SETGET_SNA = 0x5E
    SETI = 0x60
    SETC = 0x61
    GET = 0x62
    INF_REQ = 0x63
    SETGET = 0x6E
    SET_RES = 0x71
    GET_RES = 0x72
    INF = 0x73
    INFC = 0x74
    INFC_RES = 0x7A
    SETGET_RES = 0x7E


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

# EPCs requested by the initial multicast discovery. The identification number
# is needed to associate a node profile response with its source address.
DISCOVERY_INITIAL_EPCS: list[int] = [
    EPC_IDENTIFICATION_NUMBER,
    EPC_SELF_NODE_INSTANCE_LIST,
]

# EPCs requested by recurring multicast discovery. Known source addresses are
# already mapped to identification numbers, so only the instance list is needed.
DISCOVERY_DEFAULT_EPCS: list[int] = [EPC_SELF_NODE_INSTANCE_LIST]

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

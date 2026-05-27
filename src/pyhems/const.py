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

# Default EPCs for node discovery (required for identification)
DISCOVERY_DEFAULT_EPCS: list[int] = [
    EPC_IDENTIFICATION_NUMBER,
    EPC_SELF_NODE_INSTANCE_LIST,
]

# Device class codes
CLASS_CODE_HOME_AIR_CONDITIONER = 0x0130
CLASS_CODE_VENTILATION_FAN = 0x0133
CLASS_CODE_AIR_CONDITIONER_VENTILATION_FAN = 0x0134
CLASS_CODE_AIR_CLEANER = 0x0135

# Retry settings for Get requests
GET_TIMEOUT = 5.0  # Seconds to wait for response
GET_MAX_RETRIES = 3  # Maximum retry attempts for failed properties

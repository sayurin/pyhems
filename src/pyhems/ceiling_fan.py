"""Ceiling fan (class 0x013A) control writes.

The class catalog describes each property. :func:`ceiling_fan_set_properties`
builds the KDK/Panasonic write and does not send it. Fan speed, direction,
and natural wind are power plus the buzzer. A light change also sends the
Wi-Fi control source and no melody.
"""

from __future__ import annotations

from ._definitions_generated import DeviceClass
from .codecs import get_codec_for_epc
from .frame import Property

_CLASS = DeviceClass.CEILING_FAN
_POWER = 0x80
_BUZZER = 0xFC
_SPEED = 0xF0
_AIR_FLOW_DIRECTION = 0xF1
_NATURAL_WIND = 0xF2
_LIGHT = 0xF3
_LIGHT_MODE = 0xF4
_BRIGHTNESS = 0xF5
_COLOR = 0xF6
_NIGHT_LIGHTING = 0xF7
_CONTROL_SOURCE = 0xFD
_MELODY = 0xFE


def ceiling_fan_set_properties(
    *,
    power: bool,
    silent: bool = True,
    speed: str | None = None,
    air_flow_direction: str | None = None,
    natural_wind: bool | None = None,
    light: bool | None = None,
    light_mode: str | None = None,
    brightness: int | None = None,
    color: int | None = None,
    night_lighting: str | None = None,
) -> list[Property]:
    """Build the atomic SetC properties for a ceiling fan control write.

    The list always starts with operation status (``0x80``) and the buzzer
    (``0xFC``). ``silent=True`` writes ``0x31`` and does not beep.
    ``silent=False`` writes ``0x30``, which beeps for this command only.

    Power off emits only those two properties. The stored speed is left
    unchanged. Passing any other argument with ``power=False`` raises
    :class:`ValueError`.

    Power on appends each provided argument in EPC order. Several may
    travel in one SetC. An unknown enum key raises :class:`ValueError`.

    Fan speed, direction, and natural wind do not include control source or
    melody. A light change does: Wi-Fi control source and no melody. Turning
    the light on also sends light mode.
    Normal mode is used unless a night lighting level or ``night`` mode is set.

    Args:
        power: Fan power. ``True`` is on (``0x30``), ``False`` is off.
        silent: Apply this command without the confirmation beep.
        speed: Air flow rate key, ``level_1`` (10%) through ``level_10`` (100%).
        air_flow_direction: ``down`` or ``up``.
        natural_wind: Natural wind. On varies the air flow like outdoor wind.
        light: Light power.
        light_mode: ``normal`` or ``night``.
        brightness: Light level from 1 to 100.
        color: Light color. 0 is warm, 100 is cool.
        night_lighting: Night lighting level key, ``low``, ``medium``, or ``high``.

    Returns:
        Properties for one SetC. Power is first. A light change then
        inserts the Wi-Fi control source, the buzzer, and no melody.
    """
    if not power and any(
        value is not None
        for value in (
            speed,
            air_flow_direction,
            natural_wind,
            light,
            light_mode,
            brightness,
            color,
            night_lighting,
        )
    ):
        raise ValueError("Power off carries only operation status and the buzzer")

    light_change = any(
        value is not None
        for value in (light, light_mode, brightness, color, night_lighting)
    )
    if light_change and light is not False:
        if light is None:
            light = True
        if light_mode is None:
            light_mode = "night" if night_lighting is not None else "normal"

    changes: tuple[tuple[int, object], ...] = (
        (_SPEED, speed),
        (_AIR_FLOW_DIRECTION, air_flow_direction),
        (_NATURAL_WIND, natural_wind),
        (_LIGHT, light),
        (_LIGHT_MODE, light_mode),
        (_BRIGHTNESS, brightness),
        (_COLOR, color),
        (_NIGHT_LIGHTING, night_lighting),
    )
    properties = [Property(epc=_POWER, edt=_encode(_POWER, power))]
    if light_change:
        properties.append(
            Property(epc=_CONTROL_SOURCE, edt=_encode(_CONTROL_SOURCE, "wifi"))
        )
    properties.append(Property(epc=_BUZZER, edt=_encode(_BUZZER, not silent)))
    if light_change:
        properties.append(Property(epc=_MELODY, edt=_encode(_MELODY, "none")))
    if power:
        properties.extend(
            Property(epc=epc, edt=_encode(epc, value))
            for epc, value in changes
            if value is not None
        )
    return properties


def _encode(epc: int, value: object) -> bytes:
    """Encode one ceiling-fan value with the catalog codec."""
    return get_codec_for_epc(_CLASS, epc).encode(value)

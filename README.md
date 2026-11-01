# pyhems

[![Python Version](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

ECHONET Lite library for Home Energy Management System (HEMS).

**[🇯🇵 日本語ドキュメント](README.ja.md)**

## Features

- ECHONET Lite frame encoding/decoding
- UDP multicast device discovery
- Async runtime client with event subscription
- Device state management with `DeviceManager`
- Adaptive poll scheduler for non-notifying properties via `PropertyPoller`
- Entity definitions based on MRA data
- Full type hints (`py.typed`)

## Requirements

- Python 3.14+
- bidict>=0.23.0

## License

MIT License

## Installation

```bash
pip install pyhems
```

## Quick Start

```python
import asyncio
from pyhems import EOJ, HemsClient, HemsFrameEvent, HemsInstanceListEvent

async def main():
    client = HemsClient(interface="0.0.0.0")

    def on_event(event):
        if isinstance(event, HemsInstanceListEvent):
            print(f"Node: {event.node_id}, Instances: {event.instances}")
        elif isinstance(event, HemsFrameEvent):
            print(f"Frame from {event.node_id}: {event.frame}")

    unsubscribe = client.subscribe(on_event)
    await client.start()

    # Read properties from a discovered device
    # node_id = "fe..."  # obtained from HemsInstanceListEvent
    # props = await client.get(node_id, EOJ(0x013001), [0x80, 0xB3])

    # Write a property and wait for Set_Res/SetC_SNA
    # result = await client.set_property(node_id, EOJ(0x013001), 0x80, b"\x30")

    await asyncio.sleep(60)
    unsubscribe()
    await client.stop()

asyncio.run(main())
```

## Runtime API Overview

- `HemsClient.start()` / `HemsClient.stop()`: Start and stop UDP transport.
  Starting also sends the initial discovery probe and starts recurring discovery
  after 30 seconds.
- `HemsClient.subscribe(callback)`: Subscribe to runtime events.
- `HemsClient.probe_initial_nodes()`: Trigger the initial multicast node
  discovery using the identification number and self-node instance list.
- `HemsClient.probe_nodes()`: Trigger recurring multicast node discovery using
  only the self-node instance list.
- `HemsClient.start_periodic_discovery()`: Start recurring discovery immediately.
- `HemsClient.get(node_id, deoj, epcs)`: Read property values. Returns one
  `Property` per requested EPC; values that could not be read are empty.
- `HemsClient.request_get(node_id, deoj, epcs)`: Send one unsplit GET and
  return its `GetResponse`, or `None` when it is not answered.
- `HemsClient.read_manufacturer_code(node_id, deoj)`: Read EPC 0x8A on its
  own once and remember it for the device object.
- `HemsClient.set_property(node_id, deoj, epc, edt)`: Write a single property and
  wait for the acknowledged result.
- `HemsClient.set_properties(node_id, deoj, properties)`: Write multiple
  properties and wait for the acknowledged result. The returned
  `SetRequestResult` separates accepted, rejected, and unanswered EPCs. This
  low-level client API does not update a `DeviceManager` cache.
- `DeviceManager.set_properties(node_id, deoj, properties, *, refresh=False)`:
  Write through the runtime client and update cached values for accepted EPCs.
  With `refresh=True`, the accepted readable EPCs are read back shortly after
  the write so the cache reflects the value the device actually applied.
- `DeviceManager.subscribe(callback)`: Receive typed `DeviceManagerEvent`s:
  `DeviceAdded`, `PropertiesChanged` (with the changed EPCs),
  `AvailabilityChanged`, and `RuntimeActivity`. Property and availability
  events are emitted only when a value actually changes.
- `DeviceManager.last_activity_at`: Monotonic time of the latest frame received
  or device setup.
- `DeviceManager.async_start()` / `DeviceManager.async_stop()`: Subscribe to
  runtime events and process device events in arrival order. Setup of newly
  discovered devices runs in a separate task per node.

Requests to one node are sent one at a time through a per-node queue, ordered
by `RequestPriority` (Set, then setup and other reads, then polling); see
`docs/adr/0011-serialize-requests-per-node.md`.

## Adaptive Property Polling

`PropertyPoller` uses an adaptive scheduler designed for mixed ECHONET Lite devices:

- One poll cycle per device at a time, each request awaited through the node's request queue, avoids request pileups on slow devices.
- Poll cadence is adjusted from latency EWMA and failure backoff, starting from the base interval.
- Instantaneous-value EPCs can be scheduled on a separate fast lane (`fast_poll_epcs`).
- Requests are split by the per-device batch limit learned during setup (see `docs/adr/`); omitted EPCs are left to the next poll.
- Poll targets can be narrowed dynamically through `DeviceManager.subscribe_epcs(...)`.

## Definitions

```python
from pyhems import DeviceClass, REGISTRY

print(REGISTRY.version, REGISTRY.mra_version)

# Mapping: class_code -> tuple[EntityDefinition, ...]
ac_entities = REGISTRY.entities.get(DeviceClass.HOME_AIR_CONDITIONER, ())
for entity in ac_entities[:3]:
    print(entity.epc, entity.name_en)
```

`DeviceClass` is an `IntEnum` generated from the MRA `shortName` values. Custom
device classes defined in `scripts/custom_definitions.yaml` are included too.

Each `EntityDefinition` exposes `readable`/`writable` flags, enum keys (MRA
identifiers keep their MRA spelling, such as `allStop`; keys defined by pyhems,
such as `true`/`false`, are snake_case; see `docs/adr/0012-keep-mra-identifiers.md`),
its `PropertyRole`, and the value semantics derived by the generator from
the unit and the English name: `quantity` (physical quantity), `value_kind`
(measurement, total, or cumulative; `None` for static values such as ratings),
and `indication` (the condition indicated by a boolean `true`).

`get_codec_for_epc(class_code, epc, *, manufacturer_code=None, byte_offset=0)`
returns a codec converting between EDT bytes and Python values. Values whose
scale depends on sibling properties take those properties as context:

```python
codec = get_codec_for_epc(0x0287, 0xC0)
value = codec.decode(node.properties[0xC0], node.properties)
```

`encode()` raises `ValueError` for fields at a non-zero `byte_offset` and for
values outside `minimum`/`maximum`.

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
    await client.start()

    def on_event(event):
        if isinstance(event, HemsInstanceListEvent):
            print(f"Node: {event.node_id}, Instances: {event.instances}")
        elif isinstance(event, HemsFrameEvent):
            print(f"Frame from {event.node_id}: {event.frame}")

    unsubscribe = client.subscribe(on_event)
    await client.probe_initial_nodes()
    client.start_periodic_discovery()

    # Read properties from a discovered device
    # node_id = "fe..."  # obtained from HemsInstanceListEvent
    # props = await client.get(node_id, EOJ(0x013001), [0x80, 0xB3])

    # Write a property (example: power ON)
    # await client.set_property(node_id, EOJ(0x013001), 0x80, b"\x30")

    await asyncio.sleep(60)
    unsubscribe()
    await client.stop()

asyncio.run(main())
```

## Runtime API Overview

- `HemsClient.start()` / `HemsClient.stop()`: Start and stop UDP transport.
- `HemsClient.subscribe(callback)`: Subscribe to runtime events.
- `HemsClient.probe_initial_nodes()`: Trigger the initial multicast node
  discovery using the identification number and self-node instance list.
- `HemsClient.probe_nodes()`: Trigger recurring multicast node discovery using
  only the self-node instance list.
- `HemsClient.start_periodic_discovery()`: Start recurring discovery immediately.
- `HemsClient.get(node_id, deoj, epcs)`: Read property values.
- `HemsClient.set_property(node_id, deoj, epc, edt)`: Write a single property.
- `HemsClient.set_properties(node_id, deoj, properties)`: Write multiple properties.

## Adaptive Property Polling

`PropertyPoller` uses an adaptive scheduler designed for mixed ECHONET Lite devices:

- TID-correlated in-flight tracking avoids request pileups on slow devices.
- Poll cadence is adjusted from latency EWMA and failure backoff, starting from the base interval.
- Instantaneous-value EPCs can be scheduled on a separate fast lane (`fast_poll_epcs`).
- When devices truncate large responses, batch size is learned (`observed_batch_capacity`) and reused.
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

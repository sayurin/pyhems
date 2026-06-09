# pyhems

[![Python Version](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

ECHONET Lite library for Home Energy Management System (HEMS).

**[🇯🇵 日本語ドキュメント](README.ja.md)**

## Features

- ECHONET Lite frame encoding/decoding
- UDP multicast device discovery
- Async runtime client with event subscription
- Device state management with `DeviceManager`
- Poll scheduler for non-notifying properties via `PropertyPoller`
- Entity definitions based on MRA data
- Full type hints (`py.typed`)

## Requirements

- Python 3.13+
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
- `HemsClient.probe_nodes()`: Trigger multicast node discovery.
- `HemsClient.get(node_id, deoj, epcs)`: Read property values.
- `HemsClient.set_property(node_id, deoj, epc, edt)`: Write a single property.
- `HemsClient.set_properties(node_id, deoj, properties)`: Write multiple properties.

## Definitions

```python
from pyhems import REGISTRY

print(REGISTRY.version, REGISTRY.mra_version)

# Mapping: class_code -> tuple[EntityDefinition, ...]
ac_entities = REGISTRY.entities.get(0x0130, ())
for entity in ac_entities[:3]:
    print(entity.epc, entity.name_en)
```

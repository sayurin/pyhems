# pyhems

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

ECHONET Lite library for Home Energy Management System (HEMS).

**[🇯🇵 日本語ドキュメント](README.ja.md)**

## Features

- ECHONET Lite frame encoding/decoding
- UDP multicast device discovery
- Async runtime client with event subscription
- MRA (Machine Readable Appendix) data fetcher
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
from pyhems.runtime import HemsClient, HemsInstanceListEvent

async def main():
    client = HemsClient(interface="0.0.0.0")
    await client.start()

    def on_event(event):
        if isinstance(event, HemsInstanceListEvent):
            print(f"Node: {event.node_id}, Instances: {event.instances}")

    unsubscribe = client.subscribe(on_event)
    await asyncio.sleep(60)
    unsubscribe()
    await client.stop()

asyncio.run(main())
```

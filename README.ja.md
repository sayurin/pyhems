# pyhems

[![Python Version](https://img.shields.io/badge/python-3.14%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

HEMS（Home Energy Management System）向け ECHONET Lite 通信ライブラリ。

**[🇺🇸 English README](README.md)**

## 概要

pyhems は ECHONET Lite 機器との通信を行うための Python ライブラリです。
日本国内のスマートホームやエネルギー管理システムで広く採用されている ECHONET Lite プロトコルに対応しています。

### 対応機器例

- エアコン
- 空気清浄機
- 太陽光発電システム
- 蓄電池

## 特徴

- **非同期対応**: asyncio ベースの非同期 API
- **フレーム処理**: ECHONET Lite フレームのエンコード・デコード
- **デバイス検出**: UDP マルチキャストによる機器自動検出
- **イベント駆動**: コールバックによるイベント購読
- **デバイス管理**: `DeviceManager` によるノード状態管理
- **定期ポーリング**: `PropertyPoller` による通知非対応 EPC の補完
- **エンティティ定義**: MRA ベースのデバイス/エンティティ定義
- **型ヒント完備**: `py.typed` 対応

## 動作要件

- Python 3.14 以上
- bidict >= 0.23.0

## ライセンス

MIT License

## インストール

```bash
pip install pyhems
```

## 基本的な使い方

### クライアントの起動とイベント購読

```python
import asyncio
from pyhems import HemsClient, HemsFrameEvent, HemsInstanceListEvent

async def main():
    # クライアント作成（interface は利用環境に合わせて指定）
    client = HemsClient(interface="0.0.0.0")
    await client.start()

    def on_event(event):
        if isinstance(event, HemsInstanceListEvent):
            # デバイス検出イベント
            print(f"ノードID: {event.node_id}")
            print(f"インスタンス: {event.instances}")
        elif isinstance(event, HemsFrameEvent):
            # フレーム受信イベント
            print(f"フレーム受信: {event.frame}")

    unsubscribe = client.subscribe(on_event)

    # 動作中...
    await asyncio.sleep(60)

    unsubscribe()
    await client.stop()

asyncio.run(main())
```

### プロパティの読み取り（get）

```python
from pyhems import EOJ, HemsClient

async def read_properties():
    client = HemsClient(interface="0.0.0.0")
    await client.start()

    # HemsInstanceListEvent から取得した node_id を使用
    node_id = "fe..."  # 検出されたノードID

    # エアコン（0x013001）の動作状態（0x80）と設定温度（0xB3）を取得
    properties = await client.get(
        node_id=node_id,
        deoj=EOJ(0x013001),  # 家庭用エアコン インスタンス1
        epcs=[0x80, 0xB3],
    )

    for prop in properties:
        print(f"EPC: 0x{prop.epc:02X}, EDT: {prop.edt.hex()}")

    await client.stop()
```

### プロパティの書き込み（set_property / set_properties）

```python
from pyhems import EOJ, HemsClient, Property

async def write_properties(node_id: str):
    client = HemsClient(interface="0.0.0.0")
    await client.start()

    # 単一プロパティの書き込み（例: 動作状態 ON）
    await client.set_property(
        node_id=node_id,
        deoj=EOJ(0x013001),
        epc=0x80,
        edt=b"\x30",
    )

    # 複数プロパティの書き込み
    await client.set_properties(
        node_id=node_id,
        deoj=EOJ(0x013001),
        properties=[
            Property(epc=0x80, edt=b"\x30"),
            Property(epc=0xB3, edt=b"\x19"),
        ],
    )

    await client.stop()
```

### フレームの直接操作

```python
from pyhems import EOJ, Frame, Property

# フレーム作成
frame = Frame(
    tid=Frame.next_tid(),
    seoj=EOJ(0x05FF01),    # コントローラ
    deoj=EOJ(0x013001),    # エアコン
    esv=0x62,              # Get
    properties=[
        Property(epc=0x80),  # 動作状態
    ],
)

# バイト列にエンコード
data = frame.encode()

# バイト列からデコード
decoded = Frame.decode(data)
```

### EOJ クラス

EOJ（ECHONET Lite Object）は、クラスグループコード、クラスコード、インスタンス番号を管理します。

```python
from pyhems.eoj import EOJ

# 整数から作成
eoj = EOJ(0x013001)  # エアコン インスタンス1

# 属性へのアクセス
print(f"クラスコード: 0x{eoj.class_code:04X}")  # 0x0130
print(f"インスタンス: {eoj.instance}")          # 1

# バイト列に変換
eoj_bytes = eoj.to_bytes()  # b'\x01\x30\x01'

# バイト列から作成
eoj2 = EOJ.from_bytes(b'\x01\x30\x01')
```

## イベントの種類

### HemsInstanceListEvent

ノードプロファイルからのインスタンスリスト応答時に発火。

```python
@dataclass
class HemsInstanceListEvent:
    received_at: float            # 受信時刻
    instances: list[EOJ]          # EOJ のリスト
    node_id: str                  # ノード識別番号（EPC 0x83）
    properties: dict[int, bytes]  # 取得したプロパティ
```

### HemsFrameEvent

通常のフレーム受信時に発火。

```python
@dataclass
class HemsFrameEvent:
    received_at: float  # 受信時刻
    frame: Frame        # 受信フレーム
    node_id: str        # ノード識別番号
    eoj: EOJ            # 送信元 EOJ
```

### HemsErrorEvent

エラー発生時に発火。

```python
@dataclass
class HemsErrorEvent:
    received_at: float   # 発生時刻
    error: Exception     # 例外オブジェクト
```

## HemsClient の設定

```python
client = HemsClient(
    interface="0.0.0.0",     # バインドするインターフェース
    poll_interval=60.0,      # ノードプローブの間隔（秒）
    extra_epcs=[0x8A, 0x8D], # 追加で取得するEPC
)
```

- `interface`: UDP ソケットをバインドする IP アドレス
- `poll_interval`: 定期的なノード検出の間隔
- `extra_epcs`: ノードプロファイルから追加で取得する EPC（例: 0x8A=メーカーコード, 0x8D=シリアル番号）

## DeviceManager と PropertyPoller

`DeviceManager` は `HemsInstanceListEvent` / `HemsFrameEvent` を処理して、ノードごとの状態を保持します。

```python
from pyhems import DeviceManager, PropertyPoller

# class_code -> 監視するEPC集合
monitored_epcs = {
    0x0130: frozenset({0x80, 0xB3}),
}

device_manager = DeviceManager(client, monitored_epcs)
poller = PropertyPoller(device_manager, poll_interval=30.0)

poller.start()

def handle_event(event):
    if isinstance(event, HemsInstanceListEvent):
        # 新規デバイスセットアップ
        asyncio.create_task(device_manager.process_instance_list_event(event))
    elif isinstance(event, HemsFrameEvent):
        # プロパティ更新反映
        device_manager.process_frame_event(event)

unsubscribe = client.subscribe(handle_event)
```

`NodeState`（`device_manager.data[device_key]`）には以下の情報が保持されます。

- `properties`: 取得済み EPC 値（`dict[int, bytes]`）
- `get_epcs` / `set_epcs` / `inf_epcs`: 機器のプロパティマップ解析結果
- `poll_epcs`: 通知で取得できないため定期取得が必要な EPC
- `manufacturer_code` / `product_code` / `serial_number`

## エンティティ定義

pyhems は MRA（Machine Readable Appendix）データに基づいたエンティティ定義を提供します。
これにより、Home Assistant などの統合で使用するセンサーやスイッチを簡単に構成できます。

### 定義レジストリの使用

```python
from pyhems import REGISTRY

# 特定のデバイスクラスのエンティティ定義を取得
entities = REGISTRY.entities.get(0x0130, ())  # 家庭用エアコン
for entity in entities:
    print(f"{entity.name_ja}: EPC=0x{entity.epc:02X}")
```

### EntityDefinition の属性

```python
@dataclass
class EntityDefinition:
    id: str                 # 識別子（例: "class_0130_epc_bb"）
    epc: int                # プロパティコード
    name_en: str            # 英語名
    name_ja: str            # 日本語名
    get: str                # GETアクセスルール
    set: str                # SETアクセスルール
    format: str | None      # 数値フォーマット（"uint8", "int16" など）
    unit: str | None        # 単位（"W", "Celsius", "%RH" など）
    minimum: float | None   # 最小有効値
    maximum: float | None   # 最大有効値
    multiple_of: float      # スケール係数
    enum_values: tuple[EnumValue, ...]  # 列挙値
    byte_offset: int        # EDT内のバイト位置
    manufacturer_code: int | None  # メーカー固有の場合のコード
```

### デコーダファクトリ

EDT データを解釈するためのデコーダ関数を生成できます。

```python
from pyhems import create_binary_decoder, create_enum_decoder, create_numeric_decoder

# 数値デコーダ（温度: uint8, 0〜50℃）
temp_decoder = create_numeric_decoder(
    mra_format="uint8",
    minimum=0,
    maximum=50,
)
value = temp_decoder(b"\x1E")  # 30

# バイナリデコーダ（ON/OFF）
power_decoder = create_binary_decoder(on_value=b"\x30")
is_on = power_decoder(b"\x30")  # True

# 列挙型デコーダ
mode_decoder = create_enum_decoder()
mode = mode_decoder(b"\x42")  # 0x42
```

## モジュール構成

| モジュール       | 説明                            |
| ---------------- | ------------------------------- |
| `runtime`        | 高レベル通信クライアント        |
| `frame`          | フレームのエンコード・デコード  |
| `eoj`            | EOJ（オブジェクト識別子）クラス |
| `transport`      | UDP 通信層                      |
| `discovery`      | デバイス検出ユーティリティ      |
| `device_manager` | デバイス状態管理                |
| `poller`         | プロパティポーリング制御        |
| `definitions`    | エンティティ定義とデコーダ      |
| `const`          | 定数定義                        |

## 参考リンク

- [ECHONET コンソーシアム](https://echonet.jp/)

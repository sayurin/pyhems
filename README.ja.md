# pyhems

[![Python Version](https://img.shields.io/badge/python-3.13%2B-blue)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

HEMS（Home Energy Management System）向け ECHONET Lite 通信ライブラリ。

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
- **IP 非依存**: IP アドレス変更を自動追跡
- **エンティティ定義**: MRA ベースのデバイス・エンティティ定義
- **型ヒント完備**: `py.typed` 対応

## 動作要件

- Python 3.13 以上
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
from pyhems.runtime import HemsClient, HemsInstanceListEvent, HemsFrameEvent

async def main():
    # クライアント作成（インターフェースは実際のIPに変更）
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

### プロパティの読み取り（async_get）

```python
from pyhems.runtime import HemsClient
from pyhems.eoj import EOJ

async def read_properties():
    client = HemsClient(interface="0.0.0.0")
    await client.start()

    # HemsInstanceListEvent から取得した node_id を使用
    node_id = "fe..."  # 検出されたノードID

    # エアコン（0x013001）の動作状態（0x80）と設定温度（0xB3）を取得
    properties = await client.async_get(
        node_id=node_id,
        deoj=EOJ(0x013001),  # 家庭用エアコン インスタンス1
        epcs=[0x80, 0xB3],
    )

    for prop in properties:
        print(f"EPC: 0x{prop.epc:02X}, EDT: {prop.edt.hex()}")

    await client.stop()
```

### フレームの直接操作

```python
from pyhems.frame import Frame, Property
from pyhems.eoj import EOJ

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

## エンティティ定義

pyhems は MRA（Machine Readable Appendix）データに基づいたエンティティ定義を提供します。
これにより、Home Assistant などの統合で使用するセンサーやスイッチを簡単に構成できます。

### 定義レジストリの使用

```python
from pyhems import load_definitions_registry

# 定義を読み込み
registry = load_definitions_registry()

# 特定のデバイスクラスのエンティティ定義を取得
entities = registry.get_entities(0x0130)  # 家庭用エアコン
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
from pyhems import (
    create_numeric_decoder,
    create_binary_decoder,
    create_enum_decoder,
)

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

| モジュール    | 説明                            |
| ------------- | ------------------------------- |
| `runtime`     | 高レベル通信クライアント        |
| `frame`       | フレームのエンコード・デコード  |
| `eoj`         | EOJ（オブジェクト識別子）クラス |
| `transport`   | UDP 通信層                      |
| `discovery`   | デバイス検出ユーティリティ      |
| `definitions` | エンティティ定義とデコーダ      |
| `const`       | 定数定義                        |
| `utils`       | ユーティリティ関数              |

## 参考リンク

- [ECHONET コンソーシアム](https://echonet.jp/)

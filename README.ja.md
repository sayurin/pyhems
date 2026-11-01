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
- **適応ポーリング**: `PropertyPoller` による通知非対応 EPC の補完
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
    result = await client.set_property(
        node_id=node_id,
        deoj=EOJ(0x013001),
        epc=0x80,
        edt=b"\x30",
    )

    # 複数プロパティの書き込み
    result = await client.set_properties(
        node_id=node_id,
        deoj=EOJ(0x013001),
        properties=[
            Property(epc=0x80, edt=b"\x30"),
            Property(epc=0xB3, edt=b"\x19"),
        ],
    )

    await client.stop()
```

`set_property()` と `set_properties()` は `Set_Res` または `SetC_SNA` の応答を
待機し、受理・拒否・未応答のEPCを `SetRequestResult` で返します。

### フレームの直接操作

```python
from pyhems import EOJ, Frame, Property

# フレーム作成
frame = Frame(
    tid=1,
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
    discovery_interval=3600.0,  # ノードプローブの間隔（秒）
)
```

- `interface`: UDP ソケットをバインドする IP アドレス
- `discovery_interval`: 定期的なノード検出の間隔

`HemsClient.start()` は初期探索を開始し、30 秒後に定期探索を開始します。

## DeviceManager と PropertyPoller

`DeviceManager` は `HemsInstanceListEvent` / `HemsFrameEvent` を処理して、ノードごとの状態を保持します。
新しく見つかった機器の setup は、ノードごとのタスクで実行されます。setup ではメーカーコード（0x8A）を先に読むので、
`monitored_epcs` には `(class_code, manufacturer_code)` を受け取る関数も渡せます。

1 つのノードへの要求は、ノードごとのキューを通して 1 つずつ送られます。順番は `RequestPriority`
（Set、setup などの読み出し、ポーリングの順）で決まります（`docs/adr/0011-serialize-requests-per-node.md`）。

```python
from pyhems import DeviceManager, PropertyPoller

# class_code -> 監視するEPC集合
monitored_epcs = {
    0x0130: frozenset({0x80, 0xB3}),
}

device_manager = DeviceManager(client, monitored_epcs)
poller = PropertyPoller(device_manager, poll_interval=30.0)

await device_manager.async_start()
await client.start()
poller.start()

# DeviceManager が HemsInstanceListEvent / HemsFrameEvent を処理する
# ...
poller.stop()
await device_manager.async_stop()
await client.stop()
```

`DeviceManager.subscribe(callback)` で、型付きの `DeviceManagerEvent` を受け取れます。

- `DeviceAdded`: 機器の setup が完了した
- `PropertiesChanged`: キャッシュ値が変わった（変わった EPC を `epcs` に持つ）
- `AvailabilityChanged`: ポーリングによる可用性が変わった
- `RuntimeActivity`: フレームを受信した

プロパティと可用性のイベントは、値が実際に変わったときだけ通知されます。最後にフレームを受信した時刻、
または機器の setup を行った時刻は `DeviceManager.last_activity_at` で参照できます。

`DeviceManager.set_properties(..., refresh=True)` は、受理された EPC のうち読み出せるものを、書き込みの
少し後に読み直します。機器が実際に適用した値をキャッシュに反映するためです。

`PropertyPoller` は固定周期ではなく、機器状態に応じてポーリング間隔を調整します。

- 機器ごとに同時に 1 つのポーリング周期だけを実行し、ノードのキューを通して応答を待つことで、遅い機器へのリクエスト詰まりを抑制
- 応答遅延（EWMA）と連続失敗回数に応じて通常ポーリング間隔を動的に調整
- 瞬時値向け EPC（`fast_poll_epcs`）を高頻度レーンで別スケジュール
- setup で学習した同時応答数の上限（`observed_batch_capacity`）と機器ごとの GET の制約でチャンクに分けて送信し、応答に含まれなかった EPC は次の周期に任せる
- `DeviceManager.subscribe_epcs(...)` により実効ポーリング対象を動的に絞り込み

`NodeState`（`device_manager.data[device_key]`）には以下の情報が保持されます。

- `properties`: 取得済み EPC 値（`dict[int, bytes]`）
- `get_epcs` / `set_epcs` / `inf_epcs`: 機器のプロパティマップ解析結果
- `poll_epcs`: 通知で取得できないため定期取得が必要な EPC
- `inf_request_epcs` / `inf_notified_epcs` / `inf_unconfirmed_epcs`: setup で 1 回送る INF_REQ（0x63）の対象、そのうち機器が通知した EPC、拒否または応答がなかった EPC。通知された EPC はポーリングしません（`docs/adr/0009-assume-persistent-status-notifications.md`）
- `manufacturer_code` / `product_code` / `serial_number`

## エンティティ定義

pyhems は MRA（Machine Readable Appendix）データに基づいたエンティティ定義を提供します。
利用側は、この定義からセンサーやスイッチなどの表示を構成できます。

### 定義レジストリの使用

```python
from pyhems import DeviceClass, REGISTRY

# 特定のデバイスクラスのエンティティ定義を取得
entities = REGISTRY.entities.get(DeviceClass.HOME_AIR_CONDITIONER, ())  # 家庭用エアコン
for entity in entities:
    print(f"{entity.name_ja}: EPC=0x{entity.epc:02X}")
```

`DeviceClass` は MRA の `shortName` から生成される `IntEnum` です。
`scripts/custom_definitions.yaml` で定義したカスタムデバイスクラスも含まれます。

### EntityDefinition の属性

```python
@dataclass
class EntityDefinition:
    id: str                 # 識別子（例: "class_0130_epc_bb"）
    epc: int                # プロパティコード
    name_en: str            # 英語名
    name_ja: str            # 日本語名
    readable: bool          # Get で読み出せるか
    writable: bool          # Set で書き込めるか
    format: str | None      # 数値フォーマット（"uint8", "int16" など）
    unit: str | None        # 単位（"W", "Celsius", "%RH" など）
    minimum: float | None   # 最小有効値
    maximum: float | None   # 最大有効値
    multiple_of: float      # スケール係数
    enum_values: tuple[EnumValue, ...]  # 列挙値（キーは MRA の表記のまま）
    byte_offset: int        # EDT内のバイト位置
    manufacturer_code: int | None  # メーカー固有の場合のコード
    role: PropertyRole      # 機器の中での役割
    quantity: Quantity | None        # 数値が表す物理量
    value_kind: ValueKind | None     # 数値の時間的な性質（計測値、合計、積算）
    indication: Indication | None    # 真偽値の true が示す状態
```

enum のキーは、MRA に由来するものは MRA の表記（`allStop` など）のまま、pyhems が定めるもの
（`true`/`false` など）は snake_case です（`docs/adr/0012-keep-mra-identifiers.md`）。

`quantity`、`value_kind`、`indication` は、`scripts/generate_definitions.py` が単位と英語名から
導出します。`value_kind` が `None` の数値は、定格値や容量などの静的な値です。

### コーデック

`get_codec_for_epc()` は、EDT と Python の値を相互に変換するコーデックを返します。

```python
from pyhems import get_codec_for_epc

# 温度設定値（家庭用エアコン EPC 0xB3）
codec = get_codec_for_epc(0x0130, 0xB3)
codec.decode(b"\x1a")  # 26
codec.encode(27)        # b"\x1b"

# 係数を持つ値は、同じ機器オブジェクトのプロパティを context として渡す
codec = get_codec_for_epc(0x0287, 0xC0)
codec.decode(node.properties[0xC0], node.properties)
```

メーカー固有の定義や、1 つの EDT に詰められた複数の値は、`manufacturer_code` と
`byte_offset` で選びます。`encode()` は、`byte_offset` が 0 以外の値や、`minimum`/`maximum`
の範囲外の値に対して `ValueError` を送出します。

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

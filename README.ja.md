# pyhems

[![Python Version](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
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
- **MRA 対応**: Machine Readable Appendix データの取得・キャッシュ
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
            print(f"インスタンス: {[hex(eoj) for eoj in event.instances]}")
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

async def read_properties():
    client = HemsClient(interface="0.0.0.0")
    await client.start()

    # HemsInstanceListEvent から取得した node_id を使用
    node_id = "fe..."  # 検出されたノードID

    # エアコン（0x013001）の動作状態（0x80）と設定温度（0xB3）を取得
    properties = await client.async_get(
        node_id=node_id,
        deoj=0x013001,  # 家庭用エアコン インスタンス1
        epcs=[0x80, 0xB3],
    )

    for prop in properties:
        print(f"EPC: 0x{prop.epc:02X}, EDT: {prop.edt.hex()}")

    await client.stop()
```

### フレームの直接操作

```python
from pyhems.frame import Frame, Property

# フレーム作成
frame = Frame(
    tid=Frame.next_tid(),
    seoj=b"\x05\xff\x01",  # コントローラ
    deoj=b"\x01\x30\x01",  # エアコン
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

## イベントの種類

### HemsInstanceListEvent

ノードプロファイルからのインスタンスリスト応答時に発火。

```python
@dataclass
class HemsInstanceListEvent:
    received_at: float       # 受信時刻
    instances: list[int]     # EOJ のリスト
    node_id: str             # ノード識別番号（EPC 0x83）
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
    eoj: int            # 送信元 EOJ
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
    extra_epcs=[0xD5, 0xD6], # 追加で取得するEPC
)
```

- `interface`: UDP ソケットをバインドする IP アドレス
- `poll_interval`: 定期的なノード検出の間隔
- `extra_epcs`: ノードプロファイルから追加で取得する EPC

## MRA（Machine Readable Appendix）

MRA は ECHONET Lite の機器仕様を機械可読形式で提供するデータです。
pyhems では GitHub Pages からダウンロードしてキャッシュします。

### MRA の取得と利用

```python
from pyhems.mra_fetcher import MRAFetcher

fetcher = MRAFetcher()

# MRA データをダウンロード（未取得の場合のみ）
fetcher.ensure_mra()

# デバイス仕様の読み込み
device = fetcher.load_device("0x0130")  # 家庭用エアコン
print(f"クラス名: {device['className']}")

# スーパークラス（共通プロパティ）の読み込み
super_class = fetcher.load_super_class()

# 定義情報の読み込み
definitions = fetcher.load_definitions()
```

### MRA データの構成

```
~/.cache/pyhems/mra/
├── metaData.json      # バージョン情報
├── definitions/       # 共通定義
├── devices/           # デバイスクラス仕様
├── nodeProfile/       # ノードプロファイル
├── superClass/        # 共通プロパティ
└── MCRules/           # 相互接続規則
```

### MRAFetcher のメソッド

| メソッド               | 説明                       |
| ---------------------- | -------------------------- |
| `ensure_mra()`         | MRA がなければダウンロード |
| `download()`           | MRA を強制ダウンロード     |
| `is_cached`            | キャッシュ有無             |
| `needs_update()`       | 更新が必要か確認           |
| `get_local_version()`  | ローカルのバージョン       |
| `get_remote_version()` | リモートのバージョン       |
| `load_device(code)`    | デバイス仕様を読み込み     |
| `load_super_class()`   | スーパークラスを読み込み   |
| `load_definitions()`   | 定義を読み込み             |
| `clear_cache()`        | キャッシュを削除           |

## モジュール構成

| モジュール    | 説明                           |
| ------------- | ------------------------------ |
| `runtime`     | 高レベル通信クライアント       |
| `frame`       | フレームのエンコード・デコード |
| `transport`   | UDP 通信層                     |
| `discovery`   | デバイス検出ユーティリティ     |
| `mra_fetcher` | MRA データの取得・管理         |
| `const`       | 定数定義                       |
| `utils`       | ユーティリティ関数             |

## 参考リンク

- [ECHONET コンソーシアム](https://echonet.jp/)

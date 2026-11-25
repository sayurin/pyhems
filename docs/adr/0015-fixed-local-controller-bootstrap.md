# 0015. 固定ローカルノードによる Controller 起動シーケンス

- ステータス：提案
- 日付：2026-11-24

## コンテキスト

pyhems はネットワーク上の ECHONET Lite ノードを発見し、プロパティを取得する
HEMS クライアントとして動作する。一方、ECHONET Lite のノードとして認識されるには、
自ノードの Node Profile Object（0x0EF001）と Controller Object（0x05FF01）を
公開し、起動時に自ノードのインスタンスリストを通知する必要がある。

既存の通信処理は要求を送信する側に偏っており、自ノード宛ての GET / INF_REQ への
応答と、起動時の D5 通知を行っていなかった。機器の一覧や Controller Object の値を
動的に管理することは今回の目的ではなく、固定値のプロトコル応答で初期化手順を満たす
ことを優先する。

また、識別番号を固定リテラルにすると、異なるホストで pyhems を動かしたときに
Node Profile の識別番号が重複する。同一ホスト上の複数プロセスを別ノードとして
識別する必要はない。

## 決定

- pyhems は、通信ソケットの開始時に固定の Node Profile Object と Controller Object
  をローカルノードとして公開する。
- Node Profile の必須プロパティは固定値で応答する。対象は EPC 0x80、0x82、0x83、
  0x8A、0x9D、0x9E、0x9F、0xD3、0xD4、0xD6、0xD7 とする。
- Node Profile は、Controller Object の存在だけを公開する。D3 は 1、D4 は Node
  Profile と Controller の 2 クラス、D5/D6 は 0x05FF01 1 件、D7 は 0x05FF 1 件
  とする。
- Node Profile の Version 情報は ECHONET Lite Version 1.14 の固定値とする。
- 実験用のメーカーコードとして 0xFFFFFF を使う。識別番号の先頭バイトは 0xFE とし、
  続く3バイトには同じメーカーコードを入れる。
- 識別番号の残り13バイトは、`uuid.getnode()` から得たホスト識別値と bind interface
  の文字列表現を連結し、SHA-256 でハッシュ化した先頭13バイトとする。これにより、
  同一ホスト上の複数プロセスは同じ識別番号になり、通常は異なるホストで異なる識別
  番号になる。明示的な永続識別子やプロセスごとの乱数は導入しない。
- Controller Object は必須プロパティ EPC 0x80 のみを応答する。機器リスト、インデックス、
  管理対象機器の属性は公開しない。
- Node Profile の EPC 0x9D は、状変アナウンス対象として EPC 0x80 と 0xD5 を申告する。
  ただし、それらの値は固定であり、実行中の値変化を契機とする INF / INFC は送信しない。
- 起動時に Node Profile（SEOJ 0x0EF001）から D5 を OPC=1 の INF として multicast 送信する。
  EDT は Controller Object 0x05FF01 だけを含む。通信ソケットを再開した場合も、再開時の
  起動処理として同じ通知を送る。
- GET / INF_REQ の DEOJ は、実インスタンス（0x0EF001 / 0x05FF01）だけでなく、
  instance code 0x00 の全インスタンス指定（0x0EF000 / 0x05FF00）も受け付ける。
  応答の SEOJ には実在するインスタンス番号を使う。
- 未実装の Controller EPC や Set 要求には、固定値を捏造して応答しない。
- Node Profile / Controller の固定応答は、リモートノードの発見・状態管理とは分離した
  ランタイム内部機能として実装する。公開 API に明示的な初期化呼び出しを追加せず、
  既存の start / stop ライフサイクルに組み込む。

## 結果

- pyhems は起動ごとに Node Profile の D5 を通知し、GET / INF_REQ に対して最低限の
  Node Profile / Controller 情報を返せる。
- 同一ホスト上の複数プロセスは同じ ECHONET Lite ノード識別番号を共有する。これは、
  それらを別ノードとして扱わない今回の運用方針に合う。
- 異なるホストでも `uuid.getnode()` が同じ値を返す環境、MAC アドレスを取得できず
  環境依存の値が使われる環境では、識別番号の一意性を保証できない。
- Controller の機器リストや動的な状変通知は扱わないため、固定応答と実際の機器状態が
  乖離する可能性がある。動的な Controller 管理を追加するときは、この ADR を置換する
  新しい ADR を作成する。

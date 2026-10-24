# Architecture Decision Records

pyhems の設計判断を記録します。書式は Nygard 形式（ステータス／コンテキスト／決定／結果）です。

- ファイル名は `NNNN-kebab-case-title.md` とし、番号は連番にします。
- ステータスは「提案」「承認」「廃止」「置換（ADR-NNNN）」のいずれかです。
- 承認済みの ADR は書き換えません。判断を変えるときは新しい ADR を作り、古い ADR のステータスを「置換」に更新します。
- 根拠の出典や対象機種の追記は、決定を変えない補足として扱います。

| ADR | タイトル | ステータス |
|---|---|---|
| [0001](0001-all-empty-get-response.md) | 同時応答数の上限の決め方と、全 EPC が PDC=0 の GET 応答の扱い | 承認 |
| [0002](0002-avoid-fault-status-get.md) | 応答が著しく遅くなるため EPC 0x88 の自動取得を避ける | 承認 |
| [0003](0003-separate-large-distribution-board-properties.md) | パナソニックの 0x0287 では EPC 0xB3 と 0xB7 を別の GET に分ける | 承認 |
| [0004](0004-halve-get-on-timeout.md) | GET がタイムアウトしたらリクエストを半減して同時応答数を探る | 承認 |
| [0005](0005-get-responsibility-setup-and-polling.md) | 同時応答数の探索は setup で行い、ポーリングは既知の値で分割する | 承認 |
| [0006](0006-retry-incomplete-setup-on-discovery.md) | 不完全な setup を次の探索でやり直す | 承認 |

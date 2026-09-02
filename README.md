# finance-lake

個人運用の財務データ分析基盤における、レイク層（外部データソースからの取得・生データ
保存）を担うモノレポ。方針の詳細は[CLAUDE.md](./CLAUDE.md)を参照。

## サービス一覧

| サービス | 内容 |
|---|---|
| [`services/edinet-dl`](./services/edinet-dl) | EDINET（金融庁の電子開示システム）から書類ファイルを取得・保存する |

## 実行基盤

Mac Mini 2012 + Ubuntu 24.04 LTS上のDockerで動かす。共有インフラ（電源管理・ネットワーク・
systemd実行方式）のセットアップ手順は[docs/mac_mini_setup_runbook.md](./docs/mac_mini_setup_runbook.md)を参照。

## ライセンス

[MIT License](./LICENSE)

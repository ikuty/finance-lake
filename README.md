# finance-lake

個人運用の財務データ分析基盤における、レイク層（外部データソースからの取得・生データ
保存）を担うモノレポ。方針の詳細は[CLAUDE.md](./CLAUDE.md)を参照。

## サービス一覧

| サービス | 内容 |
|---|---|
| [`services/edinet-dl`](./services/edinet-dl) | EDINET（金融庁の電子開示システム）から書類ファイルを取得・保存する |
| [`services/jpx-daily-pdf-dl`](./services/jpx-daily-pdf-dl) | 日本取引所グループ（JPX）の日次株式相場表PDFを取得・保存する（設計中、個人利用限定） |

## 実行基盤

Mac Mini 2012 + Ubuntu 24.04 LTS上のDockerで動かす。共有インフラ（電源管理・ネットワーク・
systemd実行方式）のセットアップ手順は[docs/mac_mini_setup_runbook.md](./docs/mac_mini_setup_runbook.md)を参照。

## 出典表記

- EDINET由来のデータを利用・加工する際は、EDINET利用規約に従い出典表記が必須。
  例:「出典：EDINET閲覧（提出）サイト（該当ページのURL）、PDL1.0」
- 出典：日本取引所グループ 統計情報（株式関連）、
  https://www.jpx.co.jp/markets/statistics-equities/daily/index.html
  （個人利用限定。JPX利用規約により商用目的の二次利用は不可）

## ライセンス

[MIT License](./LICENSE)

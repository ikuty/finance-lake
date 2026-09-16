# finance-lake

個人運用の財務データ分析基盤における**レイク層のモノレポ**。外部データソースから
書類・データを取得し、生データのまま保存するサービス群を`services/`配下にまとめる。
メタデータの列展開・検索用インデックス作成、および中身の解釈（パース）はレイク層の
責務外とし、後段の別リポジトリ（ウェアハウス層・マート層・アプリ）が担う。

## リポジトリ構成

```
finance-lake/
├── services/             # サービスごとに自己完結（scripts/tests/docker/systemd/docs等）
├── docs/
│   └── mac_mini_setup_runbook.md   # 実行基盤（Mac Mini）自体のセットアップ
├── .github/workflows/    # サービスごとにワークフローを分け、pathsフィルタで対象を絞る
└── CLAUDE.md             # このファイル（レイヤ全体の方針）
```

サービス一覧・各サービスの詳細は[README.md](./README.md)、設計判断・実装状況は
各`services/<サービス名>/README.md`・`CLAUDE.md`を参照。

## 設計方針

- **レイク層とその後段（ウェアハウス層等）は別リポジトリ**。取得は外部APIの可用性・
  レート制限に依存し安定させたい一方、解釈ロジックは変わりやすく、変更のたびに外部へ
  再アクセスせず保存済み生データのみで再処理できることが重要なため。HTTP API/MQは
  導入せず、SQLite/ローカルファイルという共有ストレージを境界にした疎結合構成とする。
- **レイク層内のサービス同士**は変更頻度・安定性が近いため同一モノレポにまとめる。
- 依存追加は慎重に判断し、標準ライブラリで完結できるならそちらを優先する
  （over engineering回避）。Python 3.12・`mypy --strict`・`pytest`を既定とする。

## 実行基盤（共有インフラ）

- Mac Mini 2012 + Ubuntu 24.04 + Docker。OS用SSD(`/`)とストレージ用HDD(`/home`)を持ち、
  リポジトリ本体・`.env`・データは`/home`側に統一配置。データレイク本体はクラウド
  ストレージを使わない（例外: `jpx-daily-pdf-dl`のバックフィル進捗レポートのみS3で
  公開、詳細は`services/jpx-daily-pdf-dl/docs/file_download_design.md`）。
- スマートプラグ（Tapo P110M）で毎日定時に電源投入・遮断。各サービスの日次ジョブは
  電源ON〜OFFの時間枠内で処理を終えシャットダウンする必要がある。
- 実行方式はsystemdタイマーに統一（`OnCalendar`＋`Persistent=true`、`OnBootSec`は
  不採用）。シャットダウンは各サービスのunitでは行わず、共有unit
  `systemd/finance-lake-shutdown.service`が`After=`で全サービス（`finance-dwh`側の
  `finance-dwh-transform.service`含む）を列挙し待機してから実行する。
- ネットワークは外部から遮断されたLANにTailscaleを導入。開発・CIデプロイともに
  Tailscale+SSH経由。

## ブランチ戦略（2026-09-16決定）

git-flowの修正版。`main`への直接コミットは廃止。

- `main`: リリース対象のみ。`dev`: 日常の開発。`feature/*`: `dev`から派生、PRのbaseは`dev`。
- リリース時: `dev`→`release`→`main`の順にmergeしてデプロイする。

| 遷移 | マージ方式 |
|---|---|
| `feature/*` → `dev` | squash merge |
| `dev` → `release` | merge commit |
| `release` → `main` | merge commit |

GitHub上のdefault branchは`main`のまま（変更していない）。PRのbaseは都度明示的に
`dev`を指定すること（省略すると`main`向けになる）。

## 次にやること（未着手）

- `jpx-daily-pdf-dl`のバックフィル進捗レポート: Slack通知をGitHub Actions Artifact
  経由からSlack Files API（Bot Token＋`files:write`）直接送付へ変更する。
- ウェアハウス層（`finance-dwh`）: raw/cleansed層は着手済み、mart層・アプリは今後。
- **前日終値の低遅延取得**（将来着手、2026-09-06決定）: `jpx-daily-pdf-dl`のPDF日報は
  2営業日以上遅延し不適。無料代替手段（GOOGLEFINANCE・Stooq・証券会社ログイン等）は
  規約・技術制約により全て見送り済み。他サービスが一段落後、J-Quants Lightプラン
  （月額1,650円）契約で対応する方針。

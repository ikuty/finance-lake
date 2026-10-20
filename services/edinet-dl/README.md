# edinet-dl

EDINET書類一覧API・書類取得APIを日付単位で呼び出し、書類ファイルを生データのまま
取得・保存するサービス（`finance-lake`レイク層モノレポの1サービス）。メタデータの
インデックス作成・中身の解釈は後段の別リポジトリの責務とし、ここでは扱わない。

対象は、書類一覧APIの`secCode`が設定されている書類（通常の上場企業）に加え、
`formCode`が`07B`/`10B`で始まる書類（REIT＝投資法人、2026-10-11追加）、および
`seed/edinet_etf_fund_issuers.csv`の確認済みリストに一致する書類（ETF＝上場投資信託、
2026-10-17追加）。REIT・ETFはいずれも書類一覧APIの`secCode`が常に`null`になる構造的な
欠落があり、`secCode`だけでは取得対象から漏れるため別途判定している（詳細は
[docs/etf_fund_support_design.md](./docs/etf_fund_support_design.md)、REITは
[../ir-disclosure-dl/docs/reit_support_design.md](../ir-disclosure-dl/docs/reit_support_design.md)参照）。

`finance-lake`全体の方針（実行基盤・リポジトリ構成等）は[ルートのCLAUDE.md](../../CLAUDE.md)、
このサービス固有の設計判断は[CLAUDE.md](./CLAUDE.md)を参照。

以下のコマンドはすべて、このディレクトリ（`services/edinet-dl/`）で実行することを想定する。

## セットアップ

```
cp .env.example .env
# .env の EDINET_API_KEY に取得済みのAPIキーを設定する
docker build -t edinet-dl:latest -f docker/Dockerfile .
```

## 初回バックフィル（手動・1回限り）

過去10年分程度を対象に、範囲指定で実行する。中断してもfetch_progressテーブルにより
未取得日だけ再開されるので、同じコマンドを再実行すれば安全に続きから再開できる。

```
mkdir -p data
docker run --rm --env-file .env -v "$(pwd)/data:/data" edinet-dl:latest \
  --start-date 2016-08-13 --end-date 2026-08-13
```

## REIT・ETF追加バックフィル（手動・1回限り、完了済み）

`fetch_documents.py`へのREIT/ETF判定追加は今後の日次実行にのみ適用され、過去分は
再ダウンロードされない。書類一覧API自体を再度呼び出す必要はなく（`data/response/`の
生レスポンスはsecCode等で絞り込む前の全件を保存済みのため）、保存済みの一覧ファイルを
スキャンして対象書類だけを遡及取得する専用スクリプトを使う。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" --entrypoint python3 edinet-dl:latest \
  scripts/backfill_reit.py
docker run --rm --env-file .env -v "$(pwd)/data:/data" --entrypoint python3 edinet-dl:latest \
  scripts/backfill_etf.py
```

実行済み（Mac Mini実機）:

| 対象 | 処理件数 | 失敗件数（404） | ダウンロードファイル数 | サイズ |
|---|---|---|---|---|
| REIT（2026-10-11） | 725件 | 14件 | 23,062ファイル | 約1.87GB |
| ETF（2026-10-17） | 5,482件 | 166件 | 165,373ファイル | 約7.3GB |

失敗はいずれも対象書類の全形式（type=1/2/5）が404になるケース（取下げ等により書類本体が
API側から既に取得できない書類と見られる）のみで、取得ロジック自体の不具合ではない。詳細は
[docs/backfill_notes.md](./docs/backfill_notes.md)参照。

## 日次更新（systemdタイマー）

`systemd/edinet-dl.service` と `systemd/edinet-dl.timer` を参考に、Mac Miniホストの
`/etc/systemd/system/` に登録する。電源投入〜シャットダウンの時間枠内に収まるよう
`OnCalendar`の時刻を調整すること（各ファイルのコメント参照）。

```
sudo cp systemd/edinet-dl.service systemd/edinet-dl.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now edinet-dl.timer
```

`--days`は指定しない。`.env`の`DAYS_WINDOW`（既定3日）で遡り窓の日数を制御する。電源障害
等でジョブが実行されなかった日や、一時的な失敗で`error`になった日も、この窓の範囲内なら
次回実行時に自動的に再試行される。窓の終端は**前日**（今日は含めない。ジョブは営業開始前
に実行されるため、当日を対象に含めても常に0件になり、`done`確定後は当日分が永久に
再取得されなくなるため）。

`--xbrl`/`--pdf`/`--csv`で対象とする書類形式を絞り込める（いずれも未指定なら全形式が対象）。
`systemd/edinet-dl.service`では当面`--csv --pdf`のみを指定している（XBRLは対象外。詳細は
`docs/file_download_design.md`参照）。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" edinet-dl:latest --csv --pdf
```

`.env`に`SLACK_WEBHOOK_URL`を設定すると、実行結果を1回の実行につき1通Slackへ通知する。
未設定なら通知はスキップされる。

`.env`に`S3_BUCKET_NAME`（＋`AWS_ACCESS_KEY_ID`・`AWS_SECRET_ACCESS_KEY`・
`AWS_DEFAULT_REGION`）を設定すると、Slack通知の直前にバックフィル進捗レポート
（日付ごとの取得状況を年月×日の表で可視化したHTML）を生成しS3へアップロードし、
公開URLをSlack通知に含める（`jpx-daily-pdf-dl`と同じ設計・同じバケット、詳細は
[docs/file_download_design.md](./docs/file_download_design.md)参照）。未設定なら
アップロード自体をスキップする。

```
python3 scripts/backfill_report.py --db-path ... --output backfill_report.html
```

## テスト・型チェック

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
mypy
```

## 出典表記

EDINET由来のデータを利用・加工する際は、EDINET利用規約に従い出典表記が必須。
例:「出典：EDINET閲覧（提出）サイト（該当ページのURL）、PDL1.0」

## ライセンス

[MIT License](../../LICENSE)

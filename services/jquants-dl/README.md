# jquants-dl

J-Quants API（https://api.jquants.com）から複数のエンドポイントを取得し、生データの
まま保存するサービス（`finance-lake`レイク層モノレポの1サービス）。

`finance-lake`全体の方針は[ルートのCLAUDE.md](../../CLAUDE.md)を参照。

以下のコマンドはすべて、このディレクトリ（`services/jquants-dl/`）で実行する
ことを想定する。

## このサービスが取得するもの

| エンドポイント名 | URL | 内容 |
|---|---|---|
| `daily_quotes` | `GET /v2/equities/bars/daily` | 株価四本値・出来高・調整後値（株式分割等） |
| `listed_master` | `GET /v2/equities/master` | 銘柄マスタ（会社名・市場区分・業種等） |

いずれも`date`のみを指定すると、その日の全上場銘柄分（実機確認、2026-10-20:
daily_quotes 4,443件・listed_master 4,443件、ETF/REIT/インフラファンド等含む）が
1レスポンスで返る。銘柄の絞り込みはせず、レスポンスをそのまま保存する（フィルタ・
解釈は後段`finance-dwh`側の責務）。

J-Quantsは「1つの外部API（同一ドメイン・同一認証・同一レート制限予算）」という
データソース単位でこのサービス1本にまとめる方針（`jpx-daily-pdf-dl`が形式A/B/Cを
1サービスで扱うのと同じ考え方）。今後追加したいJ-Quantsのエンドポイントも、
同じ「1日1回・date指定で全銘柄分」という形であれば、`scripts/fetch_jquants_data.py`
の`ENDPOINTS`に追加するだけで対応できる。

既存の東証日報PDF取得（`jpx-daily-pdf-dl`）とは独立に収集する。日報は前場/後場別の
明細を持つがjQuantsには無く、jQuantsは株式分割等の調整後値（AdjO/AdjH/AdjL/AdjC/
AdjVo）や会社名・市場区分・業種を持つが日報の構造化は別立てのため、両者は完全な
重複ではない（詳細は[CLAUDE.md](./CLAUDE.md)参照）。

## セットアップ

```
cp .env.example .env  # JQUANTS_API_KEYを実際の値に書き換える
docker build -t jquants-dl:latest -f docker/Dockerfile .
```

## 日次更新（systemdタイマー）

```
sudo cp systemd/jquants-dl.service systemd/jquants-dl.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jquants-dl.timer
```

毎日04:01:43 JSTに発火する（`ir-disclosure-dl.timer`より後、
`finance-dwh-transform.timer`より前）。`jquants-dl.service`はマシンを
シャットダウンしない。シャットダウンは全サービス共通の共有unit
（`finance-lake-shutdown.service`、リポジトリ直下の`systemd/`配下）が一手に引き受ける。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" jquants-dl:latest
```

既定では直近`DAYS_WINDOW`日（既定3日）×全エンドポイントのうち、まだ取得していない
日・エンドポイントだけを対象にする（実行し忘れた日があっても後続の実行で自動的に
追いつく）。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" jquants-dl:latest --date 2026-10-20
docker run --rm --env-file .env -v "$(pwd)/data:/data" jquants-dl:latest --force
```

`--date`は指定日のみを対象にする（手動補完用）。`--force`は、既に取得済みの日も
再取得する。

`.env`に`SLACK_WEBHOOK_URL`を設定すると、実行結果（対象日×エンドポイントごとの
件数・成否）を1回の実行につき1通Slackへ通知する（他サービスと同じ設計）。未設定
なら通知はスキップされる。

## テスト

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m mypy
```

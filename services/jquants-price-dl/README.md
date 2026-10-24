# jquants-price-dl

J-Quants API（https://api.jquants.com）の株価四本値エンドポイントから、全上場銘柄の
当日終値を取得し、生データのまま保存するサービス（`finance-lake`レイク層モノレポの
1サービス）。

`finance-lake`全体の方針は[ルートのCLAUDE.md](../../CLAUDE.md)を参照。

以下のコマンドはすべて、このディレクトリ（`services/jquants-price-dl/`）で実行する
ことを想定する。

## このサービスが取得するもの

`GET /v2/equities/bars/daily?date=YYYYMMDD` に `date` のみを指定して呼び出すと、
その日の全上場銘柄分（実機確認、2026-10-20: 4,443件、ETF/REIT/インフラファンド等
含む）が1レスポンスで返る。銘柄の絞り込みはせず、レスポンスをそのまま保存する
（フィルタ・解釈は後段`finance-dwh`側の責務）。

既存の東証日報PDF取得（`jpx-daily-pdf-dl`）とは独立に収集する。日報は前場/後場別の
明細を持つがjQuantsには無く、jQuantsは株式分割等の調整後値（AdjO/AdjH/AdjL/AdjC/
AdjVo）を持つが日報には無いため、両者は完全な重複ではない（詳細は
[CLAUDE.md](./CLAUDE.md)参照）。

## セットアップ

```
cp .env.example .env  # JQUANTS_API_KEYを実際の値に書き換える
docker build -t jquants-price-dl:latest -f docker/Dockerfile .
```

## 日次更新（systemdタイマー）

```
sudo cp systemd/jquants-price-dl.service systemd/jquants-price-dl.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jquants-price-dl.timer
```

毎日04:01:43 JSTに発火する（`ir-disclosure-dl.timer`より後、
`finance-dwh-transform.timer`より前）。`jquants-price-dl.service`はマシンを
シャットダウンしない。シャットダウンは全サービス共通の共有unit
（`finance-lake-shutdown.service`、リポジトリ直下の`systemd/`配下）が一手に引き受ける。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" jquants-price-dl:latest
```

既定では直近`DAYS_WINDOW`日（既定3日）のうち、まだ取得していない日だけを対象にする
（実行し忘れた日があっても後続の実行で自動的に追いつく）。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" jquants-price-dl:latest --date 2026-10-20
docker run --rm --env-file .env -v "$(pwd)/data:/data" jquants-price-dl:latest --force
```

`--date`は指定日のみを対象にする（手動補完用）。`--force`は、既に取得済みの日も
再取得する。

`.env`に`SLACK_WEBHOOK_URL`を設定すると、実行結果（対象日ごとの件数・成否）を
1回の実行につき1通Slackへ通知する（他サービスと同じ設計）。未設定なら通知は
スキップされる。

## テスト

```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m mypy
```

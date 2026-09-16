# mufg-corporate-actions

三菱UFJ eスマート証券（kabu.com、旧auカブコム証券）が公開する株式分割・株式併合・商号変更
情報を取得し、生データのまま保存するサービス（`finance-lake`レイク層モノレポの1サービス）。
**個人利用限定**（kabu.com利用規約により商用利用・第三者への再配信は不可。詳細は
[CLAUDE.md](./CLAUDE.md)参照）。

`finance-lake`全体の方針は[ルートのCLAUDE.md](../../CLAUDE.md)を参照。

以下のコマンドはすべて、このディレクトリ（`services/mufg-corporate-actions/`）で実行する
ことを想定する。

## このサービスが取得するもの

- 株式分割: `https://kabu.com/investment/meigara/bunkatu.html`
- 株式併合: `https://kabu.com/investment/meigara/gensi.html`
- 商号変更: `https://kabu.com/investment/meigara/syougou_henkou.html`

3ページとも「その時点での全履歴＋今後の予定」を1ページに再掲載する形式（実機確認:
いずれも2002年8月まで遡る）。バックフィルという概念が無いため、週次で最新の全量
スナップショットを1回取得するだけでよい。

## セットアップ

```
cp .env.example .env
docker build -t mufg-corporate-actions:latest -f docker/Dockerfile .
```

## 週次更新（systemdタイマー）

```
sudo cp systemd/mufg-corporate-actions.service systemd/mufg-corporate-actions.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mufg-corporate-actions.timer
```

毎週月曜04:01:40 JSTに発火する（`jpx-daily-pdf-dl.timer`・`edinet-dl.timer`より後、
`finance-dwh-transform.timer`より前）。`mufg-corporate-actions.service`はマシンを
シャットダウンしない。シャットダウンは全サービス共通の共有unit
（`finance-lake-shutdown.service`、リポジトリ直下の`systemd/`配下）が一手に引き受ける。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" mufg-corporate-actions:latest
```

`--force`は、既に取得済みのページも再取得する（既存日の再実行用）。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" mufg-corporate-actions:latest --force
```

`.env`に`SLACK_WEBHOOK_URL`を設定すると、実行結果（3ページそれぞれの成否、ダウンロード
サイズ）を1回の実行につき1通Slackへ通知する（`edinet-dl`/`jpx-daily-pdf-dl`と同じ設計）。
未設定なら通知はスキップされる。

## テスト

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
.venv/bin/python -m mypy
```

# jpx-daily-pdf-dl

日本取引所グループ（JPX）が公開する株式相場表（東証日報）を取得し、生データのまま
取得・保存するサービス（`finance-lake`レイク層モノレポの1サービス）。**個人利用限定**
（JPX利用規約により商用目的の二次利用は不可。詳細は
[docs/file_download_design.md](./docs/file_download_design.md)参照）。

`finance-lake`全体の方針は[ルートのCLAUDE.md](../../CLAUDE.md)を参照。

以下のコマンドはすべて、このディレクトリ（`services/jpx-daily-pdf-dl/`）で実行する
ことを想定する。

## このサービスが継続的に取得するもの

- **形式C（詳細日次）**: 直近13ヶ月程度のローリングウィンドウ。市場区分・業種見出し、
  VWAP等を含む最も詳細な日次PDF。
- **形式B（月次簡易OHLC）の当年進行中の月のみ**: 月内は随時更新されるため毎回上書き
  取得する。

形式A（レガシー日次、1981〜2019年）・形式Bの確定済み過去年分（2020年〜前年まで）は、
今後増えることも変わることも無い確定データのため、このサービスの対象外。一回限りの
使い捨てスクリプトで別途取得する（未実装）。

## セットアップ

```
cp .env.example .env
docker build -t jpx-daily-pdf-dl:latest -f docker/Dockerfile .
```

## 日次更新（systemdタイマー）

```
sudo cp systemd/jpx-daily-pdf-dl.service systemd/jpx-daily-pdf-dl.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jpx-daily-pdf-dl.timer
```

`edinet-dl.timer`より前に発火するよう設定してある（`jpx-daily-pdf-dl.service`はマシンを
シャットダウンしない。`edinet-dl.service`側が最後に行う）。

`--days`は指定しない。`.env`の`DAYS_WINDOW`（既定3日）で、形式C（詳細日次）の遡り窓の
日数を制御する。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" jpx-daily-pdf-dl:latest
```

## テスト・型チェック

```
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest
mypy
```

## 出典表記

JPX由来のデータを利用する際は、リポジトリルートの[README.md](../../README.md)の出典
表記を参照。個人利用限定（商用目的の二次利用は不可）。

## ライセンス

[MIT License](../../LICENSE)

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
- **形式B（月次簡易OHLC）のうち`03.html`に現在列挙されている月**: 確定済みアーカイブへの
  移行がまだ済んでいない月（実機確認時点では約1年分の遅れがある）。列挙されている中で
  最新の月は随時更新されうるため毎回上書き取得する。

形式A（レガシー日次、1981〜2019年）・形式Bの確定済み過去年分は、今後増えることも変わる
ことも無い確定データのため、このサービスの対象外。一回限りの使い捨てスクリプトで別途
取得する（未実装）。

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

`edinet-dl.timer`より前に発火するよう設定してある。`jpx-daily-pdf-dl.service`はマシンを
シャットダウンしない。シャットダウンは全サービス共通の共有unit
（`finance-lake-shutdown.service`、リポジトリ直下の`systemd/`配下）が一手に引き受ける。

`--days`は指定しない。`.env`の`DAYS_WINDOW`（既定3日）で、形式C（詳細日次）の遡り窓の
日数を制御する。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" jpx-daily-pdf-dl:latest
```

`--force`は、DBの状態・ファイルの存在の両方を無視して必ず再ダウンロードする
（`edinet-dl`の`--force`とは意図的に異なる。edinet-dlは1日=複数ファイルという粒度
のため個々のファイルは存在すればスキップするが、jpxは1期間=1ファイルのため、その
使い分けが成立しない。単純に「現在の状態を無視して取得する」が`--force`の定義）。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" jpx-daily-pdf-dl:latest --force
```

`.env`に`SLACK_WEBHOOK_URL`を設定すると、実行結果（形式C・形式Bそれぞれの処理/成功/
失敗件数、ダウンロード件数・サイズ、リトライ発生回数、空き容量）を1回の実行につき1通
Slackへ通知する（`edinet-dl`と同じ設計）。未設定なら通知はスキップされる。

## バックフィル進捗レポート

形式A（1981-2019年）・形式B確定済み過去年分（2020年〜前月）のバックフィル状況を、
年×月の表形式（バックフィル済みなら`*`、未実施なら空欄）でHTML1枚に出力する。
バックフィルスクリプト自体は未実装だが、このレポートは先に使える（未実施なら全マス
空欄になるだけ）。Dockerを経由せず、Mac Miniホスト上のPython3から直接実行できる
（標準ライブラリのみで完結）。

```
python3 scripts/backfill_report.py --db-path /home/ikuty/finance-lake/data/jpx-daily-pdf-dl/index.db --output backfill_report.html
```

生成された`backfill_report.html`をブラウザで開いて確認する。

直近の一定期間（既定14ヶ月）は「対象範囲外」の網掛けにする。形式Cのローリング
ウィンドウ・形式Bの確定移行待ちとして常設サービスが担当する期間であり、一回限りの
バックフィルの対象ではないため。正確な確定境界が分かっている場合は`--end-year-month`
で上書きできる。

網掛け部分（常設サービス担当分）が実際にどこまで取得できているかは、月次の表とは
別に、ページ上部の日次の表（形式C、年月×日）で確認できる。

```
python3 scripts/backfill_report.py --db-path ... --end-year-month 2025-01
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

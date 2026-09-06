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
  移行がまだ済んでいない月（実機確認: 確定アーカイブは2024年12月分までで、それ以降は
  約20ヶ月の遅れがある、2026-09-06確認）。列挙されている中で最新の月は随時更新され
  うるため毎回上書き取得する。

形式A（レガシー日次、1981〜2019年）・形式Bの確定済み過去年分は、今後増えることも変わる
ことも無い確定データのため、このサービスの対象外。一回限りの使い捨てスクリプト
（[`scripts/backfill_confirmed_archive.py`](#確定済みアーカイブの一回限りバックフィル)）
で別途取得する。

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

`.env`に`S3_BUCKET_NAME`（＋`AWS_ACCESS_KEY_ID`・`AWS_SECRET_ACCESS_KEY`・
`AWS_DEFAULT_REGION`）を設定すると、Slack通知の直前にバックフィル進捗レポートを
生成しS3へアップロードし、公開URLをSlack通知に含める（詳細は後述「バックフィル
進捗レポート」参照）。未設定ならアップロード自体をスキップする。

## 確定済みアーカイブの一回限りバックフィル

形式A（1981年1月〜2019年12月）・形式B確定済み過去年分（2020年1月〜前月）を取得する
一回限りの使い捨てスクリプト。今後増えることも変わることも無い確定データのため、
systemdタイマーには含めない。Dockerを経由せず、Mac Miniホスト上のPython3から
直接実行できる（標準ライブラリのみで完結）。

```
python3 scripts/backfill_confirmed_archive.py                 # 形式A・確定済み形式Bの両方
python3 scripts/backfill_confirmed_archive.py --legacy-only    # 形式Aのみ
python3 scripts/backfill_confirmed_archive.py --confirmed-only # 確定済み形式Bのみ
python3 scripts/backfill_confirmed_archive.py --force          # 既にdoneな月も再取得
```

`DB_PATH`・`DATA_DIR`はサービス本体と共通（`.env`を読む場合は`--env-file`相当を
自分でexportするか、環境変数を直接指定する）。確定アーカイブへまだ移行していない
月（直近の一定期間）は404になるが、これはエラー扱いにせず黙ってスキップする
（次回再実行時に再チェックされる）。中断しても`fetch_progress`により未取得分だけ
再開される。1981年〜2019年分（468ヶ月）は件数が多く、完了までかなりの時間がかかる
見込み。

## バックフィル進捗レポート

形式A（1981-2019年）・形式B確定済み過去年分（2020年〜前月）のバックフィル状況を、
年×月の表形式（バックフィル済みなら`*`、未実施なら空欄）でHTML1枚に出力する。
Dockerを経由せず、Mac Miniホスト上のPython3から直接実行できる（標準ライブラリの
みで完結）。

```
python3 scripts/backfill_report.py --db-path /home/ikuty/finance-lake/data/jpx-daily-pdf-dl/index.db --output backfill_report.html
```

生成された`backfill_report.html`をブラウザで開いて確認する。

直近の一定期間（既定20ヶ月、実機確認済みの確定境界2024年12月に基づく実測値）は
「対象範囲外」の網掛けにする。形式Cのローリング
ウィンドウ・形式Bの確定移行待ちとして常設サービスが担当する期間であり、一回限りの
バックフィルの対象ではないため。正確な確定境界が分かっている場合は`--end-year-month`
で上書きできる。

網掛け部分（常設サービス担当分）が実際にどこまで取得できているかは、月次の表とは
別に、ページ上部の日次の表（形式C、年月×日）で確認できる。

```
python3 scripts/backfill_report.py --db-path ... --end-year-month 2025-01
```

**公開（S3）**: このレポートは、サービス本体（`fetch_jpx_daily.py`）の**日次実行の
たびに自動生成され、S3へアップロードされる**（`.env`に`S3_BUCKET_NAME`設定時のみ、
前述「日次更新」参照）。公開URLはSlack通知に含まれる。GitHub Actionsのデプロイ
ワークフロー経由でArtifactとして生成する旧方式は廃止した（GitHubへのログインが
無いと結果を確認できない、かつ更新頻度がデプロイ頻度に従ってしまうという設計上の
問題があったため）。バケットは静的サイトホスティングを有効化した全公開バケット
（`ikuty-finance`、秘匿情報を含まないため公開）で、ライフサイクルルールにより
7日で自動削除される。詳細は
[docs/file_download_design.md](./docs/file_download_design.md)「バックフィル進捗
レポートの公開（S3）」参照。

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

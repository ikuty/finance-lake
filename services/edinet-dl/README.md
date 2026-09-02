# edinet-dl

EDINET書類一覧API・書類取得APIを日付単位で呼び出し、書類ファイルを生データのまま
取得・保存するサービス（`finance-lake`レイク層モノレポの1サービス）。メタデータの
インデックス作成・中身の解釈は後段の別リポジトリの責務とし、ここでは扱わない。

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

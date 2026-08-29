# edinet-dl

EDINET書類一覧API・書類取得APIを日付単位で呼び出し、書類ファイルを生データのまま
取得・保存するレイク層サービス。メタデータのインデックス作成・中身の解釈は後段の
別リポジトリの責務とし、ここでは扱わない。

Mac Mini 2012 + Ubuntu 24.04 LTS上のDockerで動かす想定。マシンはOS用SSD(`/`, 250GB)と
ストレージ用HDD(`/home`, 500GB)を持つ。リポジトリ本体・`.env`・DB（`data/`、bind mount）は
すべて`/home`配下（HDD側）に置き、SSD側にはOS以外を置かない。外部ストレージ（S3等）は使わない。

マシンはスマートプラグ（Tapo P110M）で毎日定時に電源投入・遮断される。電源投入時は
`setpci`の設定によりOSが自動起動する。日次ジョブは電源ON〜OFFの時間枠内で処理を終えて
OSをシャットダウンする必要がある。ネットワークは外部から遮断されたLANに配置し、
Tailscaleでのリモートアクセス（開発時のSSH、GitHub Actionsからのデプロイ）を前提とする。

## セットアップ

```
cp .env.example .env
# .env の EDINET_API_KEY に取得済みのAPIキーを設定する
docker build -t edinet-dl:latest -f docker/Dockerfile .
```

## 初回バックフィル（手動・1回限り）

過去10年分程度を対象に、範囲指定で実行する。3,650日 × リクエスト間隔1.2秒 ≒ 70〜80分。
中断してもfetch_progressテーブルにより未取得日だけ再開されるので、同じコマンドを
再実行すれば安全に続きから再開できる。

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
次回実行時に自動的に再試行される。

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

[MIT License](./LICENSE)

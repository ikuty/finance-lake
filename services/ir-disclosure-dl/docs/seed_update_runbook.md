# 対象seedの更新手順（ランブック）

`seed/edinet_common_stock_issuers.csv`（普通株式発行体）に載っている会社だけが、日次のTDnet監視
（`run_daily.py`）とバックフィル（`backfill_tanshin.py`）の対象になる。seedは固定のため、
**新規上場と、作成時に漏れた会社は、決算短信が取得されない**（finance-dwh #106）。2026-11-22に、
seed作成（2026-09-27）後の新規上場約30社と、作成時に漏れていた三菱商事・三菱重工業・
三菱自動車工業・商船三井・YTL・メディシノバ・CKサンエツの計21社を追加した（finance-lake #45）。
この手順は、同じ漏れに気づいたときにseedへ追加し、決算短信を取得するためのもの。

## 1. 漏れの検知

finance-dwhの警告テスト `assert_cleansed__ir_disclosure__kessan_tanshin_seed_gap`（Slack通知対象）が、
日次ジョブで「上場しているのに決算短信が1件もlandingに無い銘柄」を知らせる。条件:
J-Quants銘柄マスタの最新日でプライム・スタンダード・グロース、優先株式でない、EDINETの書類がある、
決算短信のlandingに同じ4桁コードの行が無い。

- 優先株式、日本銀行、EDINETのコードが無い新規上場（EDINETの書類が出てから対象になる）は対象外。
- 新規上場は、最初の決算短信が出る前でも、EDINETの書類（届出書等）が出た時点で警告に出る。
  seedへの追加が、最初の決算短信の前に済んでいれば、日次の監視で取得される。

## 2. 追加する行を作る

警告の行（`code`=5桁、`edinet_code`）に対し、`seed` の行は `edinet_code,sec_code,filer_name`。
`filer_name` はEDINET書類一覧の提出者名。Mac Miniで、DWHのcleansedを読み取り専用で引く:

```
docker run --rm --entrypoint python \
  -v /home/<user>/finance-dwh/data:/data:ro finance-dwh-transform:latest -c "
import duckdb
for r in duckdb.sql(\"select edinet_code, sec_code, arg_max(filer_name, submit_date_time) \
from read_parquet('/data/cleansed/edinet_documents.parquet') \
where edinet_code in ('E02529') group by 1,2\").fetchall(): print(','.join(r))
"
```

（`finance-dwh-transform` のENTRYPOINTは日次フローなので、`--entrypoint python` を必ず付ける。付けないと
日次の変換が走り始める。）

## 3. seedに追加する

- `seed/edinet_common_stock_issuers.csv` に行を足す。**`sec_code` 順を保ち、改行（CRLF）をそのまま**にする。
- 追加済みの会社（`edinet_code` の重複）を足さない。seedには元々 `edinet_code` が重複する行がある
  （約4,700行で4,370社）。
- 優先株式、日本銀行、`sec_code` が不正（`00000`）の会社は足さない。
- この変更はPR（feature → dev）でマージし、**リリース（dev → release → main）とデプロイ**まで行う。
  seedはDockerイメージに焼かれる（`COPY seed/...`）ため、デプロイするまで日次の監視には効かない。

## 4. 過去分の決算短信を取得する（追加した会社のみ）

日次の監視は新しい開示しか拾わないため、過去分は `backfill_tanshin.py` で別途取得する。Mac Miniで、
**追加した会社だけの一時seed**を使い、**専用のステージング**に取得する（既存の
`staging/tanshin/_progress.json` は全社完了済みなので、使うと何も取得されない）。

```
# 一時seed: ヘッダ + 追加した行だけ
scp seed_add.csv macmini:/tmp/seed_add.csv

# 検出だけ（dry-run、ダウンロードしない）
docker run --rm --entrypoint python3 \
  -e STAGING_DIR=/data/staging/tanshin_seedfix -e RAW_DIR=/data/raw \
  -e LOG_PATH=/data/logs/backfill-tanshin-seedfix.log -e SEED_CSV_PATH=/seed/seed_add.csv \
  -v /home/<user>/finance-lake/data/ir-disclosure-dl:/data -v /tmp/seed_add.csv:/seed/seed_add.csv:ro \
  ir-disclosure-dl:latest backfill_tanshin.py --since 2024-03-01 --dry-run

# 取得（上と同じ、--dry-run なし）。21社・106件で約2分だった（失敗0）
# 昇格（stagingのrawへの移動）: 取得した月を全て指定する
docker run --rm --entrypoint python3 （同じ-e・-v） \
  ir-disclosure-dl:latest promote_staging.py --month 2026-11 --month 2026-10 ...
```

- 取得先を `raw/` ではなくステージングにするのは、`raw/` に日付フォルダが出るとDWHの夜間
  transformが取り込みを始めるため（`backfill_tanshin.py` の docstring を参照）。追加した会社だけなら
  量が少ないので、昇格は取得の直後でよい。
- 昇格のあと、日次のtransformが、レイクのdocidがlandingに揃っていない日を再取り込みする
  （finance-dwh #113）。直近7日分は日次ジョブのdbt buildの前（recent）、それ以前は**dbt buildの後**
  （backlog）に取り込まれるため、**マートに反映されるのは翌日の日次ジョブ**になる。
- 他のジョブ（19:01 UTCの日次transform、ir-disclosure-dlのタイマー）と重ならない時間に行う。

## 5. 確認

1. 日次ジョブの翌日、警告テストの行が減る（追加した会社が消える）こと。
2. MCPの `get_valuation_history`（`resample="disclosure"`）で、追加した会社のPBR・PERが入ること。
3. 一時ファイル（`/tmp/seed_add.csv`、`staging/tanshin_seedfix/`の空フォルダ）を片付ける。

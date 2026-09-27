# ir-disclosure-dl

TDnet（適時開示情報閲覧サービス）を日次監視し、対象銘柄（普通株式発行体）の
決算短信・業績予想の修正を検知する。検知した開示については、東証上場会社情報
サービス（www2.jpx.co.jp）から対応するPDFを直接取得する。`finance-lake`レイク層
モノレポ内の1サービス。設計の経緯・詳細は
[docs/jpx_disclosure_design.md](./docs/jpx_disclosure_design.md)を参照。

## セットアップ

```bash
cd services/ir-disclosure-dl
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
```

## 実行

```bash
set -a && source .env && set +a

# 日次実行（環境変数TDNET_DAYS_WINDOW分を対象）
.venv/bin/python3 scripts/run_daily.py

# 単発日付を指定（検証用）
.venv/bin/python3 scripts/run_daily.py --date 2026-09-25

# TDnet検知のみ行い、PDF取得は行わない
.venv/bin/python3 scripts/run_daily.py --skip-pdf
```

## テスト・型チェック

```bash
.venv/bin/pytest
.venv/bin/mypy
```

## データ出自

`seed/edinet_common_stock_issuers.csv`は、EDINET提出者一覧から普通株式発行体
（`sec_code`の末尾が'0'）のみを抽出したもの。列は`edinet_code,sec_code,filer_name`。

## 出典表示

- TDnetから取得するのはメタデータ（開示日時・銘柄コード・企業名・表題）のみで、
  開示PDFそのものは取得・保存しない（TDnetの二次利用・再配布禁止規約に従う）。
- 東証上場会社情報サービスから取得するPDFは、同サービス固有の免責事項
  （https://www.jpx.co.jp/listing/co-search/01.html）に基づき利用する
  （「東証は本サービスで公開している情報の利用を制限しておりません」）。

# 設計: REIT（投資法人）対応

## 背景

2026-10-07、J-Quantsを使った数値検証プロジェクト（`finance-dwh`側）で、REITの開示が
DWHに一切存在しないことから発覚（検証プロジェクト自体はREITを対象外として進めている）。
2026-10-11、原因調査（Phase 0）を実施し、対応方針を確定した。

対象REITのサンプルはアドバンス・レジデンス投資法人（edinetCode=E24347、fundCode=G07884、
証券コード3269）。

## Phase 0: 調査結果

### EDINET側（`edinet-dl`が既に取得しているデータ）

- REITの有報は`docTypeCode='120'`（通常企業の有報と同じコードを再利用）、
  `ordinanceCode='030'`（投資信託・資産流動化証券等と共有、これだけでは粗すぎる）。
- **`formCode`が実用的な判別子**: Mac Mini実データ（`ordinance_code='030'`を`formCode`で
  集計）で確認。`07B000`/`07B001`＝REITの有報/訂正有報（646件）、`10B000`＝REITの
  半期報告書、`07A*`＝投資信託・ミューチュアルファンド（REITではない、30,043件以上）、
  `082*`＝資産流動化証券、`06*`/`09*`＝その他信託。
- REITは常に単体（非連結）。実ファイルの「連結・個別」列は`{'個別','その他'}`のみ、
  DEIの`WhetherConsolidatedFinancialStatementsArePreparedDEI`も`false`。既存3モデルの
  連結/個別フォールバック機構は不要。
- XBRL→CSVの出力フォーマットは通常企業と同一（9列、UTF-16LE、タブ区切り）。
  `transform/fdw/edinet_csv_fdw.py`の変更は不要。
- 絶対額系の指標（純資産・当期純利益・総資産・営業利益等）は`jppfs_cor:`名前空間の
  個別数値ファクトとして取得可能（通常企業の生タグと共通の名前空間）。
- **1口当たり指標（EPS/BPS/分配金相当）はEDINET有報に構造化タグとして存在しない**。
  `jpsps_cor:SummaryOfBusinessResultsTextBlock`という単一の非構造化テキストブロックに
  複数期分の数値が区切り文字なしで連結された形でのみ存在し、固定幅仮定なしに安全な
  正規表現抽出は不可能（実機で全項目を網羅的に確認、CoverPageタグ含め該当する個別数値
  ファクトは一切ない）。

### TDnet/JPX側（`ir-disclosure-dl`が対象にしていないデータ）

- `ir-disclosure-dl`の対象企業seed（`seed/edinet_common_stock_issuers.csv`、4,349社）に
  REITは**1社も含まれていない**（EDINETの書類一覧APIで`secCode`が常に`null`になることが
  原因と推測、edinet-dl側の根本原因と同根）。Mac Mini実データ上、REIT法人71社・ダウン
  ロード済みデータとも突き合わせ、ir-disclosure-dlで取得されたREITの決算短信はゼロ件。
- JPX上場会社情報サービスの会社詳細ページ（`jpx_disclosure_client.fetch_company_page`）を
  REITの証券コードで取得すると、決算短信の行が通常企業とは別の`id="4101_N"`
  （通常企業は`id="1101_N"`）で返ってくる。
- **この行には、PDFへのリンクに加えてiXBRL（inline XBRL）HTMLファイルとXBRL本体(ZIP)への
  直リンクが付いている**（通常企業向けの既存パーサーはPDFしか見ていないため未使用だった）。
- 実機確認（アドバンス・レジデンス投資法人、`tse-re-t:`タクソノミ）: 1口当たり利益
  （`NetIncomePerUnitREIT`＝3,232円）・1口当たり純資産（`NetAssetsPerUnitREIT`）・
  1口当たり分配金（`DistributionsPerUnitExcludingDistributionsInExcessOfProfitREIT`＝
  3,220円）・発行済投資口総数（`NumberOfInvestmentUnitsIncludingTreasuryInvestmentUnitsIssuedAtEndOfPeriodREIT`
  ＝2,852,302口）・自己投資口数が、すべて個別の構造化タグとして存在する。かぶたん
  （https://kabutan.jp/stock/?code=3269）の1株益・1株配表示と完全一致することで実証済み。

### XBRL提供範囲の検証（5社サンプル、複数期）

全REITで取れるわけではなく、**決算短信のタイトルで明確に二分される**:

| カテゴリ | サンプル | XBRL有無（複数期で確認） |
|---|---|---|
| 「決算短信（ＲＥＩＴ）」（不動産） | 日本ビルファンド(8951)・投資法人みらい(3476)・霞ヶ関ホテルリート(401A0) | 全期間YES（日本ビルファンドは2017〜2026年の全20期で確認） |
| 「決算短信（インフラファンド）」 | カナディアン・ソーラー・インフラ(9284)・グリーンライト・再エネインフラ(509A0) | 全期間NO（インフラファンド市場はXBRL提出義務の対象外と推測） |

Mac Mini実データ上のREIT・インフラファンド71社のうち、名称に「インフラ」を含むのは7社。
残り**64社が不動産REITでXBRL取得対象**。インフラファンド7社は本設計の対象外とし、
対応するなら決算短信PDFのテキスト解析（既存の企業向けパーサーと同じregexアプローチ）で
別途検討する。

### バックフィル範囲の検証

JPXの会社詳細ページは、**1回のリクエストで会社の決算短信履歴を丸ごと返す**（ページング
不要、`docs/jpx_disclosure_design.md`に記載済みの「サーバー側は常に全部返している」仕様
通り）。日本ビルファンド（2001年上場、最古参J-REIT）で確認した実際の範囲は
**2017/02/15（平成28年12月期）〜現在の20期分**。既存の日次TDnet監視による1日ずつの
遡及バックフィル（90日分バックフィルの実績あり）とは別に、**REIT専用のワンショット
バックフィル**（会社詳細ページを1回叩くだけ）で済む。

ボリューム: 64社 × （会社詳細ページ1回＋決算短信約20期分のiXBRL/PDFダウンロード）
≈ 64 × 21 ＝ 1,344リクエスト。既存の`REQUEST_DELAY`（2秒/リクエスト）を踏襲すると
約45分。Mac Miniの電源枠内に収まる規模で、ボット対策も実機確認上見られない
（既存ir-disclosure-dl設計と同じサービス）。

## 方針（決定事項、2026-10-11）

1. **対象は不動産REIT64社のみ**。インフラファンド7社は別タスク（対応するかどうか未決定）。
2. **絶対額指標**（純資産・当期純利益・総資産等）→ `edinet-dl`が既に取得しているEDINET
   有報の`jppfs_cor:`タグから（`finance-dwh`側でのモデル追加が必要、Phase 3）。
3. **1口当たり指標**（EPS/BPS/分配金相当）→ `ir-disclosure-dl`経由でTDnet/JPXの決算短信
   iXBRL（`tse-re-t:`タクソノミ）から取得する新規実装。EDINETのテキストブロックからの
   regex抽出や、PR#58的な計算フォールバックは不要（タグそのまま使える）。
4. 過去分は、既存の日次TDnet監視を1日ずつ遡らせるのではなく、REIT専用のワンショット
   バックフィル（会社詳細ページ1回取得→決算短信履歴を丸ごと取得）で対応する。

## Phase構成（更新）

- Phase 0: 調査（完了、本ドキュメント）
- Phase 1: 実装（lake層）
  - 1a. `edinet-dl`: 書類一覧APIの`secCode is not null`フィルタを緩和し、REITの有報・
    半期報告書（`fundCode`が設定されている書類）もレイクに取得する
  - 1b. `ir-disclosure-dl`: 対象企業seedにREIT64社を追加、`jpx_disclosure_client`の
    行パーサーに`id="4101_N"`対応を追加、iXBRL/XBRL本体のダウンロードロジックを追加、
    REIT専用のワンショットバックフィルスクリプトを追加
- Phase 2: `finance-dwh`側のcleansed層修正（REITドキュメントの`sec_code is not null`
  フィルタ除去、`filer_category`に`'reit'`を追加）
- Phase 3: `finance-dwh`側に新規`intermediate__edinet__reit_financial_facts.sql`
  （絶対額指標、EDINETの`jppfs_cor:`ベース）と、ir-disclosure-dl由来の1口当たり指標
  モデル（タクソノミ`tse-re-t:`ベース）を追加
- Phase 4: mart統合（既存の`mart__edinet__financial_indicators.sql`への統合方法は
  A: 4本目のcoalesce分岐として追加 / B: 別モデル`mart__edinet__reit_financial_indicators.sql`
  のどちらにするか、Phase 3着手時に再検討）
- Phase 5: 検証

Phase 1（edinet-dl・ir-disclosure-dl、このリポジトリ）から着手する。

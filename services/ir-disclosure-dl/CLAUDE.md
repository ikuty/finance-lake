# ir-disclosure-dl

TDnet監視＋東証上場会社情報サービスからのPDF取得を担う`finance-lake`レイク層
モノレポ内の1サービス。レイヤ全体の方針はリポジトリルートの`CLAUDE.md`を参照。
このファイルはir-disclosure-dl固有の判断・実装状況を記す。

## 経緯・背景

- 会社予想EPSの取得を目的に、当初は企業自身のIRサイトを直接クロールする設計
  （`ir-url-retriever`によるローカルLLMでのURL推測＋`ir-disclosure-dl`による
  クロール）を実装したが、実機検証で複数の壁（LLMのハルシネーション、大手企業
  サイトのボット対策、DDG検索のCAPTCHA等）にぶつかり、2026-09-27に両サービスを
  一度白紙に戻した。
- 同日、東証上場会社情報サービス（www2.jpx.co.jp）がサービス固有の免責事項で
  「利用を制限しておりません」と明記していること、かつ決算短信・業績予想の修正の
  実体（PDF）を安定したURLで直接ホストしていることを発見。フォーム送信フローを
  実機解析し、スクリプトからの機械的アクセスでもボット判定を受けないことを確認
  した上で、この設計に作り直した（詳細は`docs/jpx_disclosure_design.md`参照）。

## 設計判断

- **TDnetはメタデータ検知専用**: TDnet自体は開示PDFの二次利用・再配布を禁止して
  いるため、開示PDFへのリンクは一切保存・取得しない。「いつ・どの銘柄が・何を
  開示したか」の検知のみに用いる。
- **PDF本体は東証上場会社情報サービスから取得**: 詳細は`docs/
  jpx_disclosure_design.md`のフォーム送信フロー解析を参照。
- **対象は普通株式発行体のみ**: `seed/edinet_common_stock_issuers.csv`
  （`sec_code`末尾が'0'の銘柄のみ、約4,695社）に限定する。
- **日付フォーマットのバグ修正済み（2026-09-27）**: TDnetのURLはハイフン無しの
  YYYYMMDD形式を要求するが、内部ではISO形式(YYYY-MM-DD)で日付を扱っている。
  `tdnet_client.fetch_list_page()`内で変換する（変換を忘れると1ページ目から
  404になり、検知件数が常に0件になる）。実機検証で発見・修正済み。
- **DNS解決失敗の即時失敗**: 存在しないドメインへのリトライは無駄なため、
  `socket.gaierror`は他のネットワークエラーと異なりリトライしない
  （`ir-url-retriever`開発時に実機で発見した教訓を移植）。
- **HTMLパースは正規表現のみ**: `beautifulsoup4`等は使わない。TDnet・東証上場
  会社情報サービスとも、対象箇所のHTML構造が固定的（クラス名・id命名規則が
  安定）なため、正規表現で十分と判断（依存を増やさない）。
- **実行状況レポートのS3公開（任意、2026-09-27有効化決定）**: `jpx-daily-pdf-dl`
  のバックフィル進捗レポートと同じ設計（`status_report.generate_report_html`で
  生成したHTMLをS3へアップロードし、公開URLをSlack通知に含める）。`S3_BUCKET_NAME`
  未設定なら丸ごとスキップされ、レイク本体はクラウドストレージを使わないという
  レイヤ全体の方針（ルート`CLAUDE.md`）は維持される。バケット・キーは
  `S3_BUCKET_NAME`環境変数（`ikuty-finance`を想定、他サービスと共有）・
  `run_daily.S3_REPORT_KEY`（`"ir-disclosure-dl/run_report.html"`固定）。

## Mac Mini上のパス（実行基盤）

- コード・`.env`: `/home/ikuty/finance-lake/services/ir-disclosure-dl/`
- データ（進捗DB・PDF本体）: `/home/ikuty/finance-lake/data/ir-disclosure-dl/`
  （コンテナには`/data`としてbind mount）
- systemd unit: `/etc/systemd/system/ir-disclosure-dl.service`・`.timer`
  （`OnCalendar=*-*-* 04:01:42 Asia/Tokyo`。mufg-corporate-actions(04:01:40)より後、
  finance-dwh-transform(04:01:45、finance-dwh側)より前に配置。当初04:01:50として
  いたが、dwh-transformより後に発火してしまう不具合に気づき2026-09-27修正した。
  なお現時点ではfinance-dwh側にir-disclosure-dlのデータを読むモデルが無いため
  実害は無いが、将来モデル追加時は`finance-dwh-transform.service`の`After=`に
  `ir-disclosure-dl.service`を追加する必要がある（finance-dwh側の変更、このリポジトリ
  の変更のみでは完結しない））
- Dockerイメージ: `ghcr.io/ikuty/ir-disclosure-dl:latest`

## 実装言語の選定理由

Python 3.12（stdlib中心、`boto3`のみ例外）。他のレイク層サービスと同じ判断
（`edinet-dl`のCLAUDE.md参照）。

## 次にやること（未着手）

- Mac Miniへの実デプロイ・systemdタイマー組み込み
- 実運用でのmanual_review相当（JPX側に対応する開示が見つからないケース）の
  発生率の観察

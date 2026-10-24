# jquants-dl

J-Quants API（https://api.jquants.com）から複数のエンドポイントを取得し、生データの
まま保存するサービス（`finance-lake`レイク層モノレポの1サービス）。レイヤ全体の
方針はリポジトリルートの`CLAUDE.md`を参照。このファイルはサービス固有の判断・
実装状況を記す。

**2026-10-25、`jquants-price-dl`から改名**（価格専用だった設計を、J-Quants APIの
エンドポイントを複数まとめて扱う設計に拡張したため）。

## 経緯

全銘柄の前日終値を低遅延（2営業日以上遅延する`jpx-daily-pdf-dl`の日報PDFでは不適）
で取得する手段を調査し、無料の代替手段（GOOGLEFINANCE・Stooq・証券会社ログイン・
東証上場会社情報サービスの株価情報・Alpha Vantage・Financial Modeling Prep等）を
すべて検証・却下した上で、2026-10-24にJ-Quants Lightプラン（月額1,650円）を契約した
（詳細は`[[prev-close-free-sources-exhausted]]`メモリ参照）。

当初`jquants-price-dl`として価格専用（`/v2/equities/bars/daily`）のみ実装したが、
日報との銘柄情報（会社名・市場区分・業種）差異調査（`[[jquants-price-vs-jpx-report-diff]]`
メモリ参照）で`/v2/equities/master`（銘柄マスタ）も必要になり、「J-Quantsは今後も
有用なエンドポイントを追加していきたい」というユーザーの意向を踏まえ、1サービスに
複数エンドポイントを集約する設計へ拡張した（2026-10-25決定）。

## 設計判断

- **J-Quants APIという1つの外部データソース単位で1サービスにまとめる**
  （2026-10-25決定）。データ種別（価格・銘柄マスタ・財務情報・…）ごとに別サービスを
  作る案も検討したが、既存の`jpx-daily-pdf-dl`（形式A/B/Cを1サービスで扱う）・
  `mufg-corporate-actions`（3ページを1サービスで扱う）と同じ「外部ソース単位で
  分ける」という既存の設計原則に合わせた。理由:
  1. 認証（APIキー）・レート制限予算（J-Quants Light: 60回/分）が全エンドポイント
     共通のため、サービスを分けると互いの消費量を知らないまま並行実行するリスクが
     生まれる。1サービス内の直列実行なら素直に予算を守れる。
  2. systemdタイマーの枠が極めて限られている（04:01:00〜04:01:45の45秒間に
     既に5つのタイマーが詰まっている）。エンドポイントごとに新しいタイマー・
     ワークフロー3本を増やすのは枠と運用の手間を無駄に消費する。
  3. 対象エンドポイント（価格・銘柄マスタ）はいずれも「date指定で1日1回・全銘柄分
     を1レスポンスで取得」という同じ形のスナップショット型のため、共通の
     fetch/保存/冪等性ロジックで素直に扱える。
  将来、明らかに異なる形（歴史的バックフィル前提・別プラン・重い処理等）のエンド
  ポイントが出た場合は、その時だけ個別サービスへ分離する（`edinet-dl`と
  `jpx-daily-pdf-dl`が別サービスなのは「取得先が違う」からで「データ種別が違う」
  からではない、という既存の線引きを踏襲）。
- **`scripts/fetch_jquants_data.py`の`ENDPOINTS`辞書にエンドポイントを追加するだけで
  拡張できる設計**。各エンドポイントは`{endpoint_name}.json`という独立したファイル
  に保存され、冪等性チェック（ファイル存在確認）もエンドポイント単位で独立に効く。
- **既存の`jpx-daily-pdf-dl`とは独立に収集し、cleansed層まで独立したまま持つ**
  （2026-10-24決定）。実機確認した結果、jQuantsの`daily_quotes`は日単位の始高安終・
  出来高・売買高・株式分割等調整後値（AdjO/AdjH/AdjL/AdjC/AdjVo）・時価総額を持つが、
  **前場/後場別の明細は持たない**（東証日報PDF形式Cにはある）。両者は完全な重複では
  なく、同じ取引日のデータが両ソースに存在する期間は相互検証（既存の
  `jquants-validate`と同じ発想）にも使える。どちらを正とするかはmart層以降で決める
  （今回のスコープ外）。
- **銘柄コードの桁数が日報と異なる**（実機確認、2026-10-24: jQuantsは5桁末尾0付き
  「13010」、日報は4桁「1301」）。正規化すれば銘柄の網羅性に差異は無いことを確認
  済み（`[[jquants-price-vs-jpx-report-diff]]`メモリ参照）。一方、市場区分・33業種の
  **分類方針そのもの**には実質的な差異がある（ファンド型銘柄の扱い等）。cleansed層
  実装時にこの差異を踏まえて検討する。
- **APIは1日1回のリクエストで全銘柄分を取得できる**（実機確認、2026-10-20:
  `date`のみ指定でdaily_quotes/listed_masterともに4,443件、`pagination_key`は
  出現しなかった。ETF/REIT/インフラファンド等も含めて全件返るため、本サービスは
  フィルタせずそのまま保存する。銘柄の絞り込みは後段`finance-dwh`の責務）。将来の
  件数増加・仕様変更に備え、`pagination_key`が返った場合はページを辿って連結する
  実装にしている（実機では未発生だが実装コストがほぼ0のため対応済み）。
- **listed_masterは次営業日分（17:30以降）も取得可能という仕様だが、daily_quotesと
  同じ過去窓（直近DAYS_WINDOW日）に揃える**（対称性・単純さのため、2026-10-25決定）。
- **進捗管理用のDBは持たない**。1日×1エンドポイント=1ファイル
  （`{yyyy}/{mm}/{dd}/{endpoint_name}.json`）という単純な構成のため、ファイルの
  存在チェックのみで冪等性が十分（`jquants-validate/fetch_all.py`と同じ判断。
  `mufg-corporate-actions`のような複数フォーマット・複数ステータスの管理は不要）。
- **直近`DAYS_WINDOW`日（既定3日、`edinet-dl`と同じ既定値）のローリングウィンドウ**
  で、未取得の日×エンドポイントだけを対象にする。実行時点の前日を起点にする（04時台
  に実行する前提で、前日分までが取引完了済み）。実行し忘れた日があっても後続の実行で
  自動的に追いつける（`edinet-dl`と同じ設計）。
- **バックフィル（過去分の一括取得）は今回のスコープ外**（2026-10-24決定）。
  主目的は今後の低遅延データ取得のため、本体サービスは「直近数日分だけ取得」に
  限定する。過去分が必要になった場合は別途ワンショットスクリプトを検討する。
- **バックフィル進捗レポート（HTML+S3公開）も作らない**（2026-10-24決定）。
  バックフィルが無いため、複数日に渡る進捗を可視化する対象が無い。Slack通知
  （成功/失敗・対象日×エンドポイントごとの件数）のみで足りる。
- **認証は静的APIキー方式**（V2 API、`x-api-key`ヘッダー、`jquants-validate`と
  同じ）。`services/jquants-validate/.env`の`JQUANTS_API_KEY`を使い回せる
  （同一J-Quantsアカウント）。
- **DNS解決失敗の即時失敗・429/5xxのみ指数バックオフでリトライ**
  （`ir-disclosure-dl`/`mufg-corporate-actions`と同じ方針を移植）。
- 標準ライブラリのみで完結する（S3レポート公開を行わないため`boto3`も不要、
  このリポジトリで唯一外部依存ゼロのサービス）。

## Mac Mini上のパス（実行基盤）

- コード・`.env`: `/home/ikuty/finance-lake/services/jquants-dl/`
- データ: `/home/ikuty/finance-lake/data/jquants-dl/`
  （コンテナには`/data`としてbind mount）
- systemd unit: `/etc/systemd/system/jquants-dl.service`・`.timer`
  （`OnCalendar=*-*-* 04:01:43 Asia/Tokyo`。ir-disclosure-dl(04:01:42)より後、
  finance-dwh-transform(04:01:45、finance-dwh側)より前に配置）。
  `finance-lake-shutdown.service`の`After=`と、finance-dwh側
  `finance-dwh-transform.service`の`After=`の両方にこのサービスを追加する必要が
  ある（デプロイ時に別途対応、このリポジトリの変更だけでは完結しない）。
- Dockerイメージ: `ghcr.io/ikuty/jquants-dl:latest`
- **未デプロイのため、旧名`jquants-price-dl`としての反映は行われていない**
  （改名後の`jquants-dl`名で初めてデプロイする）。

## 現状（2026-10-25時点）

- サービス本体（`scripts/fetch_jquants_data.py`）実装済み。`daily_quotes`・
  `listed_master`の2エンドポイント対応。`pytest`・`mypy --strict`ともにパス想定。
- Mac Miniへの実デプロイ・systemdタイマー登録・finance-dwh側cleansed層での利用は
  未着手。

## 次にやること（未着手）

- Mac Miniへのデプロイ（`.env`の`JQUANTS_API_KEY`設定、systemdタイマー登録、
  `finance-lake-shutdown.service`・`finance-dwh-transform.service`の`After=`更新）
- finance-dwh側で`cleansed__jquants__daily_quotes`・`cleansed__jquants__listed_master`
  相当のモデルを追加し、既存の`cleansed__jpx__stq_prices`/`cleansed__jpx__daily_ohlc`
  と並ぶ独立したcleansedテーブルとして持つ
- cleansed実装完了後、`[[jquants-price-vs-jpx-report-diff]]`メモリの調査結果
  （市場区分・33業種コードの分類方針の違い）を踏まえた調整
- 過去分バックフィルが必要になった場合のワンショットスクリプト検討
- 今後追加したい他のJ-Quantsエンドポイント（財務情報・配当等）の検討

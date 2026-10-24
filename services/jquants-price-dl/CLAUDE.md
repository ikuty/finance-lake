# jquants-price-dl

J-Quants API（https://api.jquants.com）から全上場銘柄の当日終値を取得し、生データの
まま保存するサービス（`finance-lake`レイク層モノレポの1サービス）。レイヤ全体の
方針はリポジトリルートの`CLAUDE.md`を参照。このファイルはサービス固有の判断・
実装状況を記す。

## 経緯

全銘柄の前日終値を低遅延（2営業日以上遅延する`jpx-daily-pdf-dl`の日報PDFでは不適）
で取得する手段を調査し、無料の代替手段（GOOGLEFINANCE・Stooq・証券会社ログイン・
東証上場会社情報サービスの株価情報・Alpha Vantage・Financial Modeling Prep等）を
すべて検証・却下した上で、2026-10-24にJ-Quants Lightプラン（月額1,650円）を契約した
（詳細は`[[prev-close-free-sources-exhausted]]`メモリ参照）。

## 設計判断

- **既存の`jpx-daily-pdf-dl`とは独立に収集し、cleansed層まで独立したまま持つ**
  （2026-10-24決定）。実機確認した結果、jQuantsの`/v2/equities/bars/daily`は
  日単位の始高安終・出来高・売買高・株式分割等調整後値（AdjO/AdjH/AdjL/AdjC/
  AdjVo）・時価総額を持つが、**前場/後場別の明細は持たない**（東証日報PDF形式Cには
  ある）。両者は完全な重複ではなく、同じ取引日のデータが両ソースに存在する期間は
  相互検証（既存の`jquants-validate`と同じ発想）にも使える。どちらを正とするかは
  mart層以降で決める（今回のスコープ外）。
- **APIは1日1回のリクエストで全銘柄分を取得できる**（実機確認、2026-10-20:
  `date`のみ指定で4,443件、`pagination_key`は出現しなかった。ETF/REIT/
  インフラファンド等も含めて全件返るため、本サービスはフィルタせずそのまま
  保存する。銘柄の絞り込みは後段`finance-dwh`の責務）。将来の件数増加・仕様変更に
  備え、`pagination_key`が返った場合はページを辿って連結する実装にしている
  （実機では未発生だが実装コストがほぼ0のため対応済み）。
- **進捗管理用のDBは持たない**。1日1ファイル（`{yyyy}/{mm}/{dd}/daily_quotes.json`）
  という単純な構成のため、ファイルの存在チェックのみで冪等性が十分
  （`jquants-validate/fetch_all.py`と同じ判断。`mufg-corporate-actions`のような
  複数フォーマット・複数ステータスの管理は不要）。
- **直近`DAYS_WINDOW`日（既定3日、`edinet-dl`と同じ既定値）のローリングウィンドウ**
  で、未取得の日だけを対象にする。実行時点の前日を起点にする（04時台に実行する
  前提で、前日分までが取引完了済み）。実行し忘れた日があっても後続の実行で
  自動的に追いつける（`edinet-dl`と同じ設計）。
- **バックフィル（過去分の一括取得）は今回のスコープ外**（2026-10-24決定）。
  主目的は今後の低遅延当日終値取得のため、本体サービスは「直近数日分だけ取得」に
  限定する。過去分が必要になった場合は別途ワンショットスクリプトを検討する。
- **バックフィル進捗レポート（HTML+S3公開）も作らない**（2026-10-24決定）。
  バックフィルが無いため、複数日に渡る進捗を可視化する対象が無い。Slack通知
  （成功/失敗・対象日ごとの件数）のみで足りる。
- **認証は静的APIキー方式**（V2 API、`x-api-key`ヘッダー、`jquants-validate`と
  同じ）。`services/jquants-validate/.env`の`JQUANTS_API_KEY`を使い回せる
  （同一J-Quantsアカウント）。
- **DNS解決失敗の即時失敗・429/5xxのみ指数バックオフでリトライ**
  （`ir-disclosure-dl`/`mufg-corporate-actions`と同じ方針を移植）。
- 標準ライブラリのみで完結する（S3レポート公開を行わないため`boto3`も不要、
  このリポジトリで唯一外部依存ゼロのサービス）。

## Mac Mini上のパス（実行基盤）

- コード・`.env`: `/home/ikuty/finance-lake/services/jquants-price-dl/`
- データ: `/home/ikuty/finance-lake/data/jquants-price-dl/`
  （コンテナには`/data`としてbind mount）
- systemd unit: `/etc/systemd/system/jquants-price-dl.service`・`.timer`
  （`OnCalendar=*-*-* 04:01:43 Asia/Tokyo`。ir-disclosure-dl(04:01:42)より後、
  finance-dwh-transform(04:01:45、finance-dwh側)より前に配置）。
  `finance-lake-shutdown.service`の`After=`と、finance-dwh側
  `finance-dwh-transform.service`の`After=`の両方にこのサービスを追加する必要が
  ある（デプロイ時に別途対応、このリポジトリの変更だけでは完結しない）。
- Dockerイメージ: `ghcr.io/ikuty/jquants-price-dl:latest`

## 現状（2026-10-24時点）

- サービス本体（`scripts/fetch_daily_quotes.py`）実装済み。`pytest`・
  `mypy --strict`ともにパス想定。
- Mac Miniへの実デプロイ・systemdタイマー登録・finance-dwh側cleansed層での利用は
  未着手。

## 次にやること（未着手）

- Mac Miniへのデプロイ（`.env`の`JQUANTS_API_KEY`設定、systemdタイマー登録、
  `finance-lake-shutdown.service`・`finance-dwh-transform.service`の`After=`更新）
- finance-dwh側で`cleansed__jquants__daily_quotes`相当のモデルを追加し、
  既存の`cleansed__jpx__stq_prices`/`cleansed__jpx__daily_ohlc`と並ぶ独立した
  cleansedテーブルとして持つ
- 過去分バックフィルが必要になった場合のワンショットスクリプト検討

# mufg-corporate-actions

三菱UFJ eスマート証券（kabu.com、旧auカブコム証券）が公開する株式分割・株式併合・商号変更
情報を取得し、生データのまま保存するサービス（`finance-lake`レイク層モノレポの1サービス）。
**個人利用限定**（後述の利用規約制約のため）。

レイヤ全体の方針はリポジトリルートの`CLAUDE.md`を参照。このファイルはサービス固有の
判断・実装状況を記す。

## 経緯

JPX/EDINETの株価データを扱う中で、株式分割・株式併合による過去価格の調整が未対応という
課題があった。JPXの公開情報（`markets/equities/rights/`配下）は直近12ヶ月分の遡及しか
無く、確定的な過去分バックフィルには使えないと判明していた（2026-09-14調査）。

代わりにkabu.comの以下3ページが、必要な情報を十分な歴史的深さ（3ページとも2002年8月
まで遡る）で公開していることを実機確認した（2026-09-16）:

- 株式分割: `https://kabu.com/investment/meigara/bunkatu.html`
- 株式併合: `https://kabu.com/investment/meigara/gensi.html`
- 商号変更: `https://kabu.com/investment/meigara/syougou_henkou.html`

## 利用規約についての判断（重要）

kabu.comの「投資情報に関するご注意事項」（`https://kabu.com/info/investment_advisory.html`）
に以下の記載がある（全文引用）:

> 三菱UFJ eスマート証券のホームページ上の一部情報は、東京証券取引所、大阪取引所、
> 株式会社QUICK、東洋経済新報社、日本経済新聞社、トムソン・ロイター社、リフィニティブ・
> ジャパン株式会社、ウエルスアドバイザー株式会社、株式会社フィスコ、ジャパンエコノミック
> パルス社、株式会社ミンカブ・ジ・インフォノイド、株式会社ＤＺＨフィナンシャルリサーチ、
> Cboe ジャパン株式会社、ジャパンネクスト証券株式会社、野村インベスター・リレーションズ
> 株式会社、株式会社アイフィスジャパン、ICE Data Services, Inc.、CME Group、New York
> Stock Exchange LLC、Nasdaq、S&P Dow Jones Indices LLCからの情報提供をもとに公開して
> おります。これらの情報につきましては、営業に利用することはもちろん、第三者へ提供する
> 目的で情報を加工、再利用及び再配信することを固く禁じます。

JPXの「個人利用は可」のような明示的な救済規定はこの文言中には無い。ユーザー判断により
**個人利用前提**（非商用・再配布無し）で進める（2026-09-16決定）。商用利用する場合は
その時点で有償サービスへ切り替える方針。

この制約により、**実際に取得したHTMLはgitへコミットしない**（JPX PDFと同じ扱い）。
テストは実機確認済みの列構成・区切り文字に基づく手打ちの合成HTMLフィクスチャのみ使用する。

## サービス固有の設計判断

- 3ページはいずれも「その時点での全履歴＋今後の予定」を1ページに再掲載する形式
  （EDINET/JPXのような日付ごとの独立ファイルではない。実機確認: 株式分割4049行・
  株式併合957行・商号変更1355行、いずれも2002/08/01まで遡る）。そのため
  **「バックフィル」という概念が存在せず**、週次で最新の全量スナップショットを
  1回取得すれば足りる。バックフィル進捗レポート（年月×日のマス目表示、edinet-dl/
  jpx-daily-pdf-dlにあるもの）はこの理由で実装していない。
- 保存先: `data/raw/{yyyy}/{mm}/{dd}/{bunkatu|gensi|syougou_henkou}.html`
  （日付＝実行日。週次実行のため通常は月曜日）。
- 実行頻度: 週次（毎週月曜、`OnCalendar=Mon *-*-* 04:01:40 Asia/Tokyo`）。
  実行順序はedinet-dl・jpx-daily-pdf-dlの後、finance-dwh-transformの前。
- 3ページの列構成はそれぞれ異なる（分割: 7列、併合: 5列、商号変更: 4列）が、
  レイク層はパースせず生HTMLのまま保存する（パース・型付けはDWH層の責務）。
- 標準ライブラリのみで完結（urllib.request・sqlite3・json）。S3アップロードを行わない
  ためboto3は不要（edinet-dl・jpx-daily-pdf-dlとの違い）。

## Mac Mini上のパス（実行基盤）

- コード・`.env`: `/home/ikuty/finance-lake/services/mufg-corporate-actions/`
- データ（進捗DB・HTML本体）: `/home/ikuty/finance-lake/data/mufg-corporate-actions/`
  （コンテナには`/data`としてbind mount）
- systemd unit: `/etc/systemd/system/mufg-corporate-actions.service`・`.timer`
- Dockerイメージ: `ghcr.io/ikuty/mufg-corporate-actions:latest`

## 現状（2026-09-16時点）

- サービス本体（`scripts/fetch_corporate_actions.py`）実装済み。`pytest`（13件）・
  `mypy --strict`ともにパス。

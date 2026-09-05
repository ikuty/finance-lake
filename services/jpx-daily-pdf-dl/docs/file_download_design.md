# 書類本体ダウンロード処理の実装設計（2026-09-04）

日本取引所グループ（JPX）が公開する「株式相場表」日報PDF（`stq_YYYYMMDD.pdf`）を取得し、
生データのまま保存するサービスの設計。`finance-lake`レイク層モノレポ内の1サービス
（`services/jpx-daily-pdf-dl/`）として、`services/edinet-dl/`と並置する。

## 経緯・利用規約についての判断

- 東証の日々の株式相場表PDF（例:
  https://www.jpx.co.jp/markets/statistics-equities/daily/t13vrt000001v7aw-att/stq_20260901.pdf ）
  をCSV化する一回限りの作業を行った際、JPXサイトの利用規約
  （https://www.jpx.co.jp/term-of-use/）を確認したところ、以下の制約があることが分かった。
  - 著作権はJPXに帰属し、無断改変は禁止
  - 有償契約またはJPXの許可がない限り、**商用目的によるデータ収集及び二次利用はできない**
  - 生成AI等による学習・解析・生成利用について、権利侵害や「JPXが不利益を被る可能性のある
    一切の行為」を禁止する包括条項がある
  - EDINETのように「AI利用制限は無い」と明言されているわけではなく、グレーゾーンがある
- **本サービスは個人利用限定とし、配布・商用利用は行わない**。配布・商用利用の価値が
  出てきた場合は、JPXの有償データサービス（JPxData Portal等）への切り替えを検討する。
  無償の一次情報である当データは、あくまで個人の投資判断参考目的の範囲で使用する。
- 出典表記をリポジトリルートの`README.md`に記載する（詳細は後述）。

## 対象範囲

- **日次の株式相場表PDF（`stq_YYYYMMDD.pdf`）のみ**。他の統計情報（月間相場表、統計月報等）
  は対象外。
- 立会市場・ToSTNeT市場等、PDF内部の区分は問わず、PDFファイルそのものを丸ごと生データとして
  保存する（中身の解釈・CSV化は後段の別リポジトリの責務。レイク層はファイルの取得・格納に
  特化する、というfinance-lake全体の方針に準拠）。

## 取得方法（2段構成、スクレイピングを最小化）

東証はEDINETのような「書類取得API」を持たない。日次PDFの実際のURLは、JPXのCMSが発行する
不透明なコンテンツID（例: `t13vrt000001v7aw-att`）を含んでおり、**日付から機械的に導出する
ことはできない**（実機で複数の日付のURLを確認し、規則性が無いことを確認済み）。

このため、以下の2段構成で取得する。

1. **一覧ページの取得**（月1回程度の頻度で十分）
   - 直近数日分: `https://www.jpx.co.jp/markets/statistics-equities/daily/index.html`
   - 過去分（月次アーカイブ）: `https://www.jpx.co.jp/markets/statistics-equities/daily/00-archives-{NN}.html`
     （`NN`は`01`〜`12`、`01`が直近の月、`12`が最も古い月）
   - ページ内の`stq_YYYYMMDD.pdf`を含むリンクを正規表現等で抽出し、日付→実際のPDF URL の
     対応表を作る（HTMLパースはこの一覧ページに対してのみ行う。書類本体はURL指定で直接
     取得するため、書類そのものへのスクレイピングは発生しない）。
   - **実機確認**: `00-archives-12`は2025年9月分（最古）までしかカバーしておらず、
     **アーカイブによる遡り取得は概ね直近1年程度が上限**（EDINETの10年バックフィルとは
     大きく異なる制約）。ページ数が増えていく様子は無く、新しい月が追加されると古い月が
     ローテーションで見えなくなると推測される。
2. **PDF本体の取得**
   - 1で判明した実際のURLへ直接HTTPリクエストし、PDFをそのまま保存する。

## 保存先パス

```
data/raw/{yyyy}/{mm}/{dd}/stq.pdf
```

`edinet-dl`が採用した`{yyyy}/{mm}/{dd}`の3階層と揃える（レイク層内のサービス間で規約を
統一し、後段の日次ingestが両サービスに対して同じパターンでアクセスできるようにするため）。
ファイル名を`stq_{yyyymmdd}.pdf`ではなく`stq.pdf`とするのは、日付が既にディレクトリ階層に
含まれているため冗長になるのを避けるため（`edinet-dl`の`document_list.json`と同じ考え方）。

## PDFの扱い

**生PDFのまま保存する。CSV変換・パースは行わない**（レイク層の責務外、後段の別リポジトリが
担う）。以前の一回限りの検証作業でCSV変換を試みた際、`pdftotext -layout`＋正規表現パーサーで
構造的に99.95%抽出できることを確認済みだが、その変換ロジック自体をレイク層に持ち込む必要は
無い。

## 冪等性・進捗管理

`edinet-dl`の`fetch_progress`とは別に、本サービス専用の進捗テーブルを新設する（DBファイルも
サービスごとに分離、`data/edinet-dl/`とは独立した`data/jpx-daily-pdf-dl/`配下に置く）。

```sql
CREATE TABLE fetch_progress (
    fileDate TEXT PRIMARY KEY,
    status TEXT,       -- 'done' | 'error'
    sourceUrl TEXT,     -- 実際に取得したPDFの完全なURL（出典表示・監査証跡用）
    message TEXT,
    fetchedAt TEXT
)
```

`sourceUrl`列は`edinet-dl`の`fetch_progress`には無い、本サービス固有の追加列。JPXのCMS
コンテンツIDを含む実際のURLはダウンロード後に再現できないため、取得の都度記録しておく
（利用規約上の出典表示、および後日「本当にこのURLから取得したものか」を追跡する監査証跡
としての価値がある）。

`stq.pdf`ファイル自体の存在チェックによる冪等性は、`edinet-dl`の`save_atomic`パターン
（一時ファイル→rename）をそのまま踏襲する。

## リトライ・実行スケジュール

- **リトライ設計**: `edinet-dl`と同じ2段階（実行内: 429・5xx・ネットワークエラーを最大5回
  指数バックオフ／実行間: 日付単位の進捗テーブル＋数日分の遡り窓）を踏襲する。
- **実行基盤**: 同じMac Mini上、systemdタイマーで実行する。`edinet-dl.timer`
  （`OnCalendar=*-*-* 04:01:30 Asia/Tokyo`）と電源投入〜シャットダウンの同じ時間枠
  （04:00〜06:00 JST）内に収める必要があるため、`jpx-daily-pdf-dl.timer`は時刻をずらして
  設定する（具体的な時刻は実装時に決定。1日1ファイルのみの軽量な処理のため、実行時間は
  短い見込み）。
- **HTTP接続**: `edinet-dl`が導入したKeep-Alive接続（`EdinetHttpClient`）は、1日あたり
  数百回のリクエストが発生する状況向けの最適化だった。本サービスは1日あたり月1回の一覧
  ページ取得＋1回のPDF取得程度で頻度が低く、Keep-Alive最適化の恩恵は薄いと判断し、
  `urllib.request`をそのまま使う素朴な実装で十分とする（over engineering回避）。

## 出典表記

リポジトリルートの`README.md`に、EDINETと同様の形式で1文追加する。

```
出典：日本取引所グループ 統計情報（株式関連）、
https://www.jpx.co.jp/markets/statistics-equities/daily/index.html
```

個別ファイルごとの実際の取得元URL（一覧の列挙）はREADME.mdには含めない（日数分だけ肥大化
するため）。進捗テーブルの`sourceUrl`列に構造化データとして保持する（前述）。

## テスト方針

`edinet-dl`と同じ方針（新規テスト用ライブラリを追加せず、`unittest.mock`＋`pytest`の
`tmp_path`/`monkeypatch`で書く）。一覧ページのHTMLパース処理は、実際に取得したHTMLの
断片をテスト用フィクスチャとして保持し、リンク抽出ロジックを検証する。

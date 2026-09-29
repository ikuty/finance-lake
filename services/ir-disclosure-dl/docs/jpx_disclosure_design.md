# 設計: TDnet監視 + 東証上場会社情報サービスからのPDF取得

## 背景・経緯

会社予想EPS（次期業績予想の1株当たり利益）はEDINETに構造化データとして存在せず、
決算短信・適時開示側にのみ存在する。この情報を取得する方法として、以下を検討し、
いずれも見送った経緯がある（詳細は2026-09-27のセッション記録参照）。

1. **企業自身のIRサイトを直接クロール**（ローカルLLMでURL推測＋fetch検証）:
   実装したが実機検証で、LLMの推測誤り（存在しないドメインの推測、非定型URL構造の
   見落とし）、Akamai等の大手企業サイトのボット対策（正しいURLでも403）、DNS解決
   失敗の無駄なリトライ等、複数の壁にぶつかり撤回した。
2. **DuckDuckGo検索での代替**: html版はボット判定でチャレンジページ、lite版も
   数クエリでCAPTCHA（「アヒルの絵を選択」）が出て使用不可と判明。CAPTCHA回避は
   ポリシー上実装できない。

## 採用した設計

- **TDnet（release.tdnet.info）**: 「いつ・どの銘柄が・何を開示したか」という
  メタデータの検知専用に使う。TDnet自体は開示PDFの二次利用・再配布を禁止して
  いるため、TDnet側のPDFリンクは一切保存・取得しない。
- **東証上場会社情報サービス（www2.jpx.co.jp/tseHpFront/）**: 実際のPDF取得元。
  このサービス固有の免責事項（`https://www.jpx.co.jp/listing/co-search/01.html`
  下部）には「東証は本サービスで公開している情報の利用を制限しておりません」と
  明記されており、サイト全体の利用規約（二次利用・再配信を原則禁止）とは別に、
  この特定サービスの情報については利用が制限されていない（2026-09-27確認）。
  スクリプトからの機械的アクセス（curlのデフォルトUA含む）でもボット判定なしに
  200 OKで応答することを実機確認済み。

## 東証上場会社情報サービスのフォーム送信フロー（実機解析、2026-09-27）

Struts系のdispatchパターン（`<input type="hidden" name="X" value="X" disabled>`が
複数あり、JSの`submitPage(form, field)`が押されたボタンに対応する1つだけを
`disabled=false`にしてから`form.submit()`する）。

```
1. GET  https://www2.jpx.co.jp/tseHpFront/JJK010010Action.do?Show=Show
   → セッションCookie(JSESSIONID)確立

2. POST https://www2.jpx.co.jp/tseHpFront/JJK010010Action.do
   body: ListShow=ListShow&sniMtGmnId=&mgrMiTxtBx=&eqMgrCd=<5桁sec_code>&dspSsuPd=10
   → 銘柄コードでの検索結果（1件ヒットする前提）

3. POST https://www2.jpx.co.jp/tseHpFront/JJK010030Action.do
   body: BaseJh=BaseJh&lstDspPg=1&dspGs=10&souKnsu=1&sniMtGmnId=JJK010010&
         dspJnKbn=0&dspJnKmkNo=0&mgrCd=<5桁sec_code>&jjHisiFlg=1
   → 銘柄詳細ページ。基本情報・適時開示情報・縦覧書類等、全タブの内容が
     1レスポンスに含まれる（JSでタブ切り替えしているだけで、サーバー側は
     常に全部返している）。
```

いずれも同一セッション（Cookie）で行う必要がある。

## 適時開示情報[決算情報]の抽出

ステップ3のレスポンスHTML内、`<tr id="1101_N">`という行だけが[決算情報]カテゴリ
（決算短信・四半期決算短信・業績予想の修正・配当予想の修正・決算IR説明会資料等、
121か月分）に該当することを実機確認済み。他カテゴリ（[決定事実/発生事実]等）は
1102・1105・1106・1107・1109等の別プレフィックスを使うため、外側のtable境界を
追わずこのidの数字部分だけで安全に絞り込める。

各行から開示日・表題・PDFへの直接リンク（`/disc/{5桁コード}/{文書ID}.pdf`）を
抽出する。このPDF URLは認証・セッション不要で直接ダウンロード可能（スクリプトから
の機械的アクセスでもボット判定なし、実機確認済み）。

TDnetで検知したイベント(event_date, disclosure_kind)に対応する行は、まずTDnetの
表題と完全一致するものを優先して採用する（JPXは通常TDnetと同一の表題をそのまま
掲載しているため）。完全一致が無い場合のみ、同じ種別のキーワード（"決算短信"／
"業績予想の修正"）を含み、開示日が最も近い（前後3日以内）ものにフォールバックする。

同一銘柄が同日・同種別で複数件の開示を出すケース（実機確認、2026-09-27: 北海電力
(95090)が同日同時刻に「業績予想(連結)の修正に関するお知らせ」と「2026年度 連結
業績予想の修正について」という2件の別文書を提出）では、日付近似だけでは区別でき
ず、当初は異なるTDnetイベントが同じJPX側PDFに誤って紐付くバグがあった。表題の
完全一致判定と、同一銘柄内で既に他イベントへ割り当て済みのPDFを除外する仕組み
（`jpx_disclosure_client.select_matching_disclosure`の`exclude_pdf_urls`）で修正
した。

## JPX側掲載の遅延と再試行設計（2026-09-29追加）

JPX上場会社情報サービスへの掲載は、TDnetでの当日開示から1日以上遅れることがある
（実機確認、2026-09-29）。当初の実装は、PDF取得を試みた時点でJPX側に対応する
開示が見つからない場合、即座に`pdf_downloads`へ`status='skipped'`として確定記録
していた。`db.pending_tdnet_events`は「`pdf_downloads`に行が無いイベント」だけを
再試行対象とする設計のため、この確定記録により当該イベントは二度と再試行され
ず、JPX側が翌日以降に掲載しても永久に取得漏れとなるバグがあった（JPXページ取得
自体が一時的なネットワークエラーで失敗した場合の`status='error'`確定記録も同様の
問題を抱えていた）。

対応として、`run_daily.MAX_PENDING_RETRY_DAYS`（既定7日）を超えるまでは、
マッチ不成立・JPXページ取得失敗・PDFダウンロード失敗のいずれについても
`pdf_downloads`へ何も記録しない（`db.record_pdf_download`を呼ばない）ように
変更した。これにより`db.pending_tdnet_events`の既存の仕組みにそのまま乗り、
翌日以降のジョブ実行で自動的に再試行される。猶予日数を超えても解決しない場合の
み、最終的に`skipped`／`error`として確定記録し、再試行対象から外す。件数は
`RunStats.pdf_pending_retry`（Slack通知の「再試行待ち」）・`status_report.py`の
「pending（再試行待ち）」行で可視化する。

## レイクへの格納・メタデータJSON（2026-09-27追加）

PDF本体は`{DATA_DIR}/{yyyy}/{mm}/{dd}/{edinet_code}/{JPX側の文書ID}.pdf`に保存する
（`edinet-dl`等と同じ、日付3階層＋`edinet_code`のレイク配置規約）。

PDFのファイル名はJPX側の内部文書IDであり、それだけではどの銘柄・どの開示種別
（決算短信／業績予想の修正）のPDFかを判別できない。後段（`finance-dwh`）はこの
サービスのSQLite進捗DB（`ir_disclosure.db`）を読まない設計（レイク層のファイルを
globするだけで完結させる、`edinet-dl`の`document_list.json`と同じ理由）のため、
PDFと同じ場所に同名の軽量メタデータJSON（`{同じ文書ID}.json`）を一緒に着地させる
（`run_daily.build_pdf_metadata`）。内容: `edinet_code`・`sec_code`・
`company_name`・`disclosure_kind`・`tdnet_event_date`・`tdnet_kj_time`・
`tdnet_title`・`jpx_disclosure_date`・`jpx_title`・`pdf_url`。

## 既知のリスク

この一連の手順はJPXの公開ページの内部実装（フォームのフィールド名・隠しdispatch
フィールド・行のid命名規則等）に依存した実装であり、公式に文書化されたAPIでは
ない。JPX側のサイトリニューアル等で構造が変わった場合、動作しなくなる可能性が
ある（実際、2022年時点の同種の解析記事とは`<table>`のインデックス等が既に
変わっていることを確認済み）。動作しなくなった場合は、本ファイルに記録した
実機確認済みの構造を起点に、再度ブラウザで手動確認しながら追従する。

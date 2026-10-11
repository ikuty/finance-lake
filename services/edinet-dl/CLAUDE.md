# edinet-dl

EDINET（金融庁の電子開示システム）から書類ファイルを取得し、生データのまま保存する
`finance-lake`レイク層モノレポ内の1サービス。レイヤ全体の方針（リポジトリ構成・実行基盤・
言語方針等）はリポジトリルートの`CLAUDE.md`を参照。このファイルはedinet-dl固有の判断・
実装状況を記す。

## 経緯・背景

- 元々は `ikuty-skills` リポジトリ内の `stock-fundamental-analysis` スキルのデータ取得部分として着手した。
- 当初 IR Bank (irbank.net) をデータソース候補としたが、**利用規約でAI・機械学習目的での利用が明示的に禁止**されており（取得方法を問わず）、採用を断念した。
- 代替として **EDINET API** を採用。金融庁公式・無償・規約上AI利用の制限なし。ただし以下の制約がある:
  - Webサイトの直接スクレイピングは禁止。**書類取得APIの利用はこの禁止の適用除外として明示的に許可**されているため、必ずAPI経由でアクセスすること
  - 短時間の大量アクセスは禁止（運用上の礼儀としてリクエスト間隔を空ける）
  - **出典表示が必須**。例:「出典：EDINET閲覧（提出）サイト（該当ページのURL）、PDL1.0」。加工した場合は加工した旨と作成者名も明記
  - AI・機械学習目的の利用制限は無い（IR Bankとの決定的な違い）
- スキルという単位から独立させ、単独リポジトリ`edinet-dl`として切り出した（2026-08-13）。
- レイク層に複数サービスが増える見込みとなり、`finance-lake`モノレポの1サービス
  （`services/edinet-dl/`）として再編した（2026-09-02）。

## edinet-dl固有の設計判断

- **書類本体（実データ）の保存**（実装済み）: 対象は全上場企業（`secCode`が設定されている書類）。500GBのHDDに対して見積もり上十分な余裕があるため、クラウドへのアップロードや取得後の削除は行わない。バックアップ（HDD故障時の復旧手段）は2026-09-23、USB外付けHDD（500GB）へのresticによる週次手動バックアップとして導入した。対象は`data/edinet-dl`を含むレイク生データ全体（`finance-dwh`側の導出データは対象外）。手順は`docs/backup_runbook.md`参照。
  - 保存先パス: `data/{fileDate}/{edinetCode}/{type}/{docID}.pdf`（PDF）、`data/{fileDate}/{edinetCode}/{type}/{docID}/（元のzip内パス）.gz`（XBRL/CSV、展開して個別gzip圧縮）。日付を最上位階層にするのは、後段の日次ingestが対象日のディレクトリだけを見れば済むようにするため。`edinetCode`を次の階層にするのは、人間がレイクを直接見たときに企業単位で判別しやすくするため。type（xbrl/csv/pdf）をさらに次の階層にするのは、後段でtype単位の一括読み込みをしやすくするため。docIDを最下層に置くのは、同一企業・同一日に複数docIDがある場合、展開後のファイル名（EDINET側の命名は書類種別ごとに同名になりやすい）が衝突するのを防ぐため。詳細は`docs/file_download_design.md`参照。
  - 大量保有報告書等、提出者の`edinetCode`と報告対象企業の`subjectEdinetCode`が異なる書類では、提出者側のディレクトリに格納される（現時点では許容）。
  - 書類一覧APIの生レスポンスも`data/response/document_list_{fileDate}.json`として保存する（詳細は`docs/file_download_design.md`参照）。
- **DBの役割**: SQLite(`fetch_progress`テーブル)は日付ごとの取得状況（済み/エラー、件数）の記録専用。書類メタデータの列展開・検索用インデックスは持たない（後段リポジトリの責務）。
- **日次ジョブ**: `--days`は指定せず、環境変数`DAYS_WINDOW`（既定3日）による遡り窓で実行する。電源障害等でジョブが実行されなかった日や、一時的な失敗で`error`になった日も、窓の範囲内なら自動的に再試行される。
- **初回バックフィル**: 過去分を手動実行（systemdタイマーには含めない）。`fetch_progress`テーブルにより中断・再開可能。
- **取得対象形式**: `--xbrl`/`--pdf`/`--csv`で絞り込み可能。日次運用では当面`--csv --pdf`のみ（XBRLは対象外。理由は`docs/file_download_design.md`参照）。
- **HTTP接続**: `EdinetHttpClient`でKeep-Alive接続を使い回す（2026-09-01導入、詳細は`docs/file_download_design.md`参照）。API呼び出しの並列化・`REQUEST_DELAY`のさらなる削減は、利用規約の「短時間の大量アクセス禁止」との整合性の観点から見送っている。
- **「今日」の判定**: `today_jst()`でJST基準の日付を使う。コンテナのシステムTZ（UTC）で`datetime.date.today()`を使うと、`edinet-dl.timer`の発火時刻（JST 04:01:30＝UTC前日19:01:30）の関係でDAYS_WINDOWが1日ずれるバグがあった（2026-08-31修正）。
- **バックフィル進捗レポート**（`scripts/backfill_report.py`、2026-09-08追加）: 日付ごとの取得状況（2016-08-13〜前日）を年月×日のコンパクトな表で可視化する。`jpx-daily-pdf-dl`と異なりedinet-dlは単一粒度（日次のみ、形式の切り分けが無い）のため表は1つで済む。日次実行のたびにSlack通知の直前で生成しS3（`ikuty-finance`バケット、キー`edinet-dl/backfill_report.html`）へアップロードし、公開URLをSlack通知に含める（設計・実装は`jpx-daily-pdf-dl`と同じ、詳細は`docs/file_download_design.md`「バックフィル進捗レポートの公開（S3）」参照）。
- **実機不具合の記録（2026-09-08、修正済み）**: `/etc/systemd/system/edinet-dl.service`に、リポジトリの雛形ファイル由来の未置換プレースホルダ`<user>`が残ってしまい、2026-09-06・09-07の2日間`docker run`自体が起動直後に失敗し続けた（`--env-file`のパスが存在しないため）。原因は、2026-09-06の共有シャットダウンunit導入作業でこのunitファイルをリポジトリの雛形でそのまま上書きした際、実際のユーザー名`ikuty`への置換を忘れたこと。`sudo sed -i 's/<user>/ikuty/g'`で修正し、手動`docker run`で欠損2日分を回収した。教訓: 雛形ファイルを`/etc/systemd/system/`へコピーする際は、必ず`<user>`が実際の値に置換されているか確認すること。

## Mac Mini上のパス（実行基盤）

- コード・`.env`: `/home/ikuty/finance-lake/services/edinet-dl/`
- データ（進捗DB・書類本体）: `/home/ikuty/finance-lake/data/edinet-dl/`（コンテナには`/data`としてbind mount）
- systemd unit: `/etc/systemd/system/edinet-dl.service`・`edinet-dl.timer`
- Dockerイメージ: `ghcr.io/ikuty/edinet-dl:latest`（リポジトリ名変更後もこのイメージ名は維持）
- 電源: スマートプラグ（Tapo P110M）でON 04:00 JST / OFF 06:00 JST。`edinet-dl.timer`は`OnCalendar=*-*-* 04:01:30 Asia/Tokyo`。

## 実装言語の選定理由

Python 3.12（stdlib中心）を採用。本ジョブはEDINET APIへのI/Oバウンドな処理で、かつ意図的にリクエスト間隔を空けているため、省メモリ・高速実行の軸はほぼ効かないと判断。Go等への書き換えは、動作確認済みのコードを捨てて存在しない性能問題を解決することになり、over engineeringと判断して見送った。
- 依存管理: 標準ライブラリのみ（依存ゼロ）で完結している方針だったが、バックフィル進捗レポートのS3アップロードのため`boto3`（AWS公式SDK）を例外的に追加した（2026-09-08、`jpx-daily-pdf-dl`と同じ判断。AWS Signature V4の自前実装は複雑で保守コストが高いため）。

## 現状（2026-09-02時点）

- 実装・デプロイ済み。日次ジョブ（`--csv --pdf`）が稼働中。
- バックフィル完了分: 2026年5月・6月・7月・8月・9月（進行中）。
- Keep-Alive接続（`EdinetHttpClient`）導入済み（1リクエストあたり約32%短縮）。
- `finance-lake`モノレポへの再編作業中（このコミット時点でリポジトリ内の移動は完了、Mac Mini側・GitHubリポジトリ名変更は未実施）。

## 次にやること（未着手）

- GitHubリポジトリ名の変更（`edinet-dl`→`finance-lake`）
- Mac Mini側の実ディレクトリ・`.env`・systemd unitの新パスへの移行
- GitHub Actionsワークフローのリネーム・pathsフィルタ追加
- `docs/definitions`（docTypeCodeごとの標準タクソノミ要素定義）の作成（設計方針は合意済み、未着手）
- **REIT（投資法人）対応**（2026-10-07判明、2026-10-11 Phase 0完了・Phase 1着手）: 現状、
  書類一覧APIの`secCode`で絞り込んでいるため（本ファイル冒頭「対象はsecCodeが設定されている
  書類」参照）、REITの書類は一度もレイクに取得されていない。EDINET上、投資法人には書類一覧
  APIの`secCode`が付与されない（常に`null`）ため。
  ただし、XBRL本体のDEI項目（`jpdei_cor:SecurityCodeDEI`＝「証券コード、DEI」）には、通常の
  株式会社と全く同じ形式で証券コードが入っている（実機確認: アドバンス・レジデンス投資法人
  (edinetCode=E24347, fundCode=G07884)の第28期有報で値`"32690"`＝証券コード3269の5桁表記）。
  このため、書類一覧APIの`fundCode`が設定されている書類も取得対象に含めれば、DWH側の
  `intermediate__edinet__dei_facts`の既存ロジックは改修なしで証券コードを取得できる見込み。
  財務数値のうち絶対額系（純資産・当期純利益等）は`jppfs_cor:`名前空間（通常企業の生タグと
  共通）から取得可能だが、1口当たり指標（EPS/BPS相当）はEDINET有報に構造化タグが存在しない
  ことがPhase 0調査で確定した（代わりに`ir-disclosure-dl`経由のTDnet/JPX決算短信iXBRLを使う
  設計にした）。Phase 0の詳細な調査結果・方針・Phase構成は
  `services/ir-disclosure-dl/docs/reit_support_design.md`参照。
  （J-Quantsを使った数値検証プロジェクトの一環で、REITの開示がDWHに一切存在しないことから
  発覚した。検証プロジェクト自体はREITを対象外として進める。）

## 参照

- EDINET API仕様: `type=5`指定でXBRLをCSV化した形で取得可能（UTF-16LE、タブ区切り）。生XBRL/XMLパースより大幅に簡単。
- 有報CSVのデータカタログ（項目一覧・注意点）は `ikuty-skills` リポジトリの
  `skills/stock-fundamental-analysis/references/edinet_yuho_csv_catalog.md` に詳細あり。
  特に「連結/個別の混在」はコンテキストIDの完全一致判定が必須（`_NonConsolidatedMember`サフィックスの有無で区別、`startswith`等の部分一致は危険）という実務上の注意点は要参照。

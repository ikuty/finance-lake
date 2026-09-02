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

- **書類本体（実データ）の保存**（実装済み）: 対象は全上場企業（`secCode`が設定されている書類）。500GBのHDDに対して見積もり上十分な余裕があるため、クラウドへのアップロードや取得後の削除は行わない。バックアップ（HDD故障時の復旧手段）は別途検討する。
  - 保存先パス: `data/{fileDate}/{edinetCode}/{type}/{docID}.pdf`（PDF）、`data/{fileDate}/{edinetCode}/{type}/{docID}/（元のzip内パス）.gz`（XBRL/CSV、展開して個別gzip圧縮）。日付を最上位階層にするのは、後段の日次ingestが対象日のディレクトリだけを見れば済むようにするため。`edinetCode`を次の階層にするのは、人間がレイクを直接見たときに企業単位で判別しやすくするため。type（xbrl/csv/pdf）をさらに次の階層にするのは、後段でtype単位の一括読み込みをしやすくするため。docIDを最下層に置くのは、同一企業・同一日に複数docIDがある場合、展開後のファイル名（EDINET側の命名は書類種別ごとに同名になりやすい）が衝突するのを防ぐため。詳細は`docs/file_download_design.md`参照。
  - 大量保有報告書等、提出者の`edinetCode`と報告対象企業の`subjectEdinetCode`が異なる書類では、提出者側のディレクトリに格納される（現時点では許容）。
  - 書類一覧APIの生レスポンスも`data/response/document_list_{fileDate}.json`として保存する（詳細は`docs/file_download_design.md`参照）。
- **DBの役割**: SQLite(`fetch_progress`テーブル)は日付ごとの取得状況（済み/エラー、件数）の記録専用。書類メタデータの列展開・検索用インデックスは持たない（後段リポジトリの責務）。
- **日次ジョブ**: `--days`は指定せず、環境変数`DAYS_WINDOW`（既定3日）による遡り窓で実行する。電源障害等でジョブが実行されなかった日や、一時的な失敗で`error`になった日も、窓の範囲内なら自動的に再試行される。
- **初回バックフィル**: 過去分を手動実行（systemdタイマーには含めない）。`fetch_progress`テーブルにより中断・再開可能。
- **取得対象形式**: `--xbrl`/`--pdf`/`--csv`で絞り込み可能。日次運用では当面`--csv --pdf`のみ（XBRLは対象外。理由は`docs/file_download_design.md`参照）。
- **HTTP接続**: `EdinetHttpClient`でKeep-Alive接続を使い回す（2026-09-01導入、詳細は`docs/file_download_design.md`参照）。API呼び出しの並列化・`REQUEST_DELAY`のさらなる削減は、利用規約の「短時間の大量アクセス禁止」との整合性の観点から見送っている。
- **「今日」の判定**: `today_jst()`でJST基準の日付を使う。コンテナのシステムTZ（UTC）で`datetime.date.today()`を使うと、`edinet-dl.timer`の発火時刻（JST 04:01:30＝UTC前日19:01:30）の関係でDAYS_WINDOWが1日ずれるバグがあった（2026-08-31修正）。

## Mac Mini上のパス（実行基盤）

- コード・`.env`: `/home/ikuty/finance-lake/services/edinet-dl/`
- データ（進捗DB・書類本体）: `/home/ikuty/finance-lake/data/edinet-dl/`（コンテナには`/data`としてbind mount）
- systemd unit: `/etc/systemd/system/edinet-dl.service`・`edinet-dl.timer`
- Dockerイメージ: `ghcr.io/ikuty/edinet-dl:latest`（リポジトリ名変更後もこのイメージ名は維持）
- 電源: スマートプラグ（Tapo P110M）でON 04:00 JST / OFF 06:00 JST。`edinet-dl.timer`は`OnCalendar=*-*-* 04:01:30 Asia/Tokyo`。

## 実装言語の選定理由

Python 3.12（stdlib中心）を採用。本ジョブはEDINET APIへのI/Oバウンドな処理で、かつ意図的にリクエスト間隔を空けているため、省メモリ・高速実行の軸はほぼ効かないと判断。Go等への書き換えは、動作確認済みのコードを捨てて存在しない性能問題を解決することになり、over engineeringと判断して見送った。
- 依存管理: 標準ライブラリのみ（依存ゼロ）で完結している。クラウドSDK（`boto3`等）は不要。

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

## 参照

- EDINET API仕様: `type=5`指定でXBRLをCSV化した形で取得可能（UTF-16LE、タブ区切り）。生XBRL/XMLパースより大幅に簡単。
- 有報CSVのデータカタログ（項目一覧・注意点）は `ikuty-skills` リポジトリの
  `skills/stock-fundamental-analysis/references/edinet_yuho_csv_catalog.md` に詳細あり。
  特に「連結/個別の混在」はコンテキストIDの完全一致判定が必須（`_NonConsolidatedMember`サフィックスの有無で区別、`startswith`等の部分一致は危険）という実務上の注意点は要参照。

# Mac Miniへのデプロイ設計（2026-08-25）

GitHub Actions（Tailscale+SSH経由）によるMac Miniへのデプロイ方式の設計。CLAUDE.mdの
「ネットワーク」の項に記載済みの方針（GitHub ActionsもTailscale+SSHで本機に接続し、
モジュール等をデプロイする）を具体化したもの。実装（`.github/workflows/`）はこの
リポジトリのスコープ外として別途行う。

## 構成: ビルドとデプロイを分離する

Mac Miniは電源スケジュール（スマートプラグTapo P110Mによる日次ON/OFF）で稼働時間が
限られており、常時Tailscale経由で到達可能とは限らない。一方、Dockerイメージのビルドと
レジストリへのpushはMac Miniの状態に依存しない。この非対称性を踏まえ、2つの工程を分離する。

### 1. ビルド＆push（自動トリガー）

`main`ブランチへのpushをトリガーに自動実行する。

```
1. Dockerイメージをビルド
2. ghcr.io（GitHub Container Registry）へ latest タグでpush
```

GitHubのアカウント/リポジトリと同じエコシステムであるghcr.ioを使うことで、新しい外部サービス
（別のクラウドアカウント等）を増やさない。GitHub Actionsからは`GITHUB_TOKEN`でそのまま
認証できる。

### 2. デプロイ（Mac Miniへの反映、手動トリガー）

`workflow_dispatch`による手動トリガーとする。Mac Miniの電源が入っており、Tailscale経由で
到達可能であることをユーザーが確認してから実行する運用とする（自動push時にMac Miniの電源
が入っているとは限らないため、自動トリガーにはしない）。

```
1. Tailscale+SSHでMac Miniに接続
2. docker pull ghcr.io/<owner>/edinet-dl:latest
```

`docker run --rm`方式（コンテナ内にデーモンを置かない実行方式）のため、pull後の再起動や
サービス再読み込みは不要。次回のsystemdタイマー実行から新しいイメージが使われる。

## イメージのタグ戦略

`latest`タグのみを使う。コミットSHA別のタグ付け・ロールバック機構は持たない（運用の手間に
対して個人運用の規模では過剰と判断）。ロールバックが必要な場合は、該当コミットに戻して
再度ビルド＆pushする。

## ghcr.ioの認証

パッケージを**公開**に設定する。Dockerイメージにはソースコードのみが含まれ、APIキー等の
秘密情報は一切含まれない（`EDINET_API_KEY`等は`docker run --env-file`で実行時に渡す）ため、
公開にしても実害はない。これによりMac Mini側はレジストリの認証情報を一切持たずに
`docker pull`できる。管理すべき秘密情報を増やさない、という既存方針に沿う。

## SSH鍵の管理

GitHub Actions Secretsに秘密鍵を保存し、ワークフロー内でTailscale経由のSSH接続に使用する。

## GitHubリポジトリの公開設定

**公開リポジトリ**とする（2026-08-25決定）。ghcr.ioパッケージを公開にする方針と対をなす。
リポジトリを公開する前に、秘密情報の混入がないことを確認済み:

- `.env`/`.env.local`（実際のAPIキーを含む）は`.gitignore`で除外されており、`git status`上も
  コミット対象に含まれていないことを確認した
- リポジトリ内のどのファイルにもAPIキーの実値がハードコードされていないことをgrepで確認した
- スクラッチ用途だった`sample_documents.txt`（`documents`テーブルのサンプル出力、既に削除
  済みの`documents`テーブル由来）を削除した。内容自体はEDINETの公開情報で機密性はないが、
  用済みのファイルとして整理した

## 秘密情報の継続的なチェック（gitleaks）

公開前の手動チェック（grepによる既知のキー値の確認）は一度きりのものであり、将来のコミット
に対する保護にはならない。公開リポジトリという性質上、今後のコミットで誤って秘密情報が
混入した場合の影響は大きいため、`gitleaks`（単機能・低メンテナンスのシークレットスキャン
ツール）をCIに組み込む。

- **トリガー**: `main`へのpush。個人運用でmainに直接pushする運用（PRを介さない）を想定して
  いるため、`push`トリガーのみで十分。将来PRベースの運用に変える場合は`pull_request`
  トリガーも追加し、マージ前に検知できるようにするとより安全
- **過去履歴の遡及スキャン**: 不要。2026-08-25時点でリポジトリはまだ1コミットもない
  （`git status`で"No commits yet"）ため、最初のコミットから将来に向けてクリーンな状態を
  維持すればよい
- **実装**: `gitleaks/gitleaks-action`（公式GitHub Action）を使う。公開リポジトリでは
  無料で動作し、追加のアカウント・認証情報は不要（既存の「秘密情報を増やさない」方針に沿う）
- **位置づけ**: イメージのビルド＆pushワークフローとは別の、独立した軽量ワークフロー
  （1ジョブ）とする。疎結合にしておき、互いに依存させない

## まとめ

| 工程 | トリガー | Mac Mini到達性への依存 |
|---|---|---|
| ビルド＆push（ghcr.io） | `main`へのpush（自動） | 依存しない |
| デプロイ（`docker pull`） | `workflow_dispatch`（手動） | 依存する（ユーザーが電源投入を確認してから実行） |

## 実装状況

- `.github/workflows/build-push.yml`・`deploy.yml`・`gitleaks.yml`: 実装済み（2026-08-25）
- リモートリポジトリ作成・初回push: 完了（https://github.com/ikuty/edinet-dl、公開）

## セットアップ完了状況（2026-08-26時点）

- **Secrets**: `SSH_PRIVATE_KEY`・`MAC_MINI_HOST`・`MAC_MINI_USER`・`TS_OAUTH_CLIENT_ID`・
  `TS_OAUTH_SECRET`の5つとも設定済み。TailscaleのOAuthクライアントは`tag:ci`スコープの
  `Auth Keys`（Write）で作成（管理画面の「Devices」ではなく「Keys」カテゴリ配下）
- **Mac Mini実機**: 初期セットアップ完了（[mac_mini_setup_runbook.md](./mac_mini_setup_runbook.md)参照）。
  Tailscale・Docker導入済み、systemdタイマー登録・実機での動作確認（`docker run`→成功時の
  シャットダウン→次回電源投入時の`setpci`自動起動）まで確認済み
- **ghcr.ioパッケージの可視性**: `build-push.yml`実行・確認済み（2026-08-26）。公開リポジトリに
  紐づくパッケージは`GITHUB_TOKEN`でのpush時点で自動的にPublicになっており、想定していた
  「手動でPublicに切り替える」作業は不要だった（当初の想定が誤りだったため、この節を修正）
- 初回10年分バックフィルの実行: 未対応

## 不具合修正の記録: `deploy.yml`のイメージタグ不一致（2026-08-27）

`deploy.yml`初回実行時、`docker pull`は成功したが、実際に`edinet-dl.service`が起動するイメージが
更新されないという不具合を実機で発見した。

**原因**: `deploy.yml`は`ghcr.io/<owner>/edinet-dl:latest`というタグでpullしていたが、
`edinet-dl.service`のExecStartは（`mac_mini_setup_runbook.md`の初回セットアップでローカル
ビルドした際に付けた）レジストリ接頭辞の無い`edinet-dl:latest`というタグを参照していた。
Dockerではこの2つは別のローカルタグとして扱われるため、`docker pull`で新しいイメージを
取得しても、systemdサービスが参照する`edinet-dl:latest`タグ自体は古いイメージを指したまま
だった。`docker images`で2つのタグが別々のIMAGE IDを指していることを確認して発覚した。

**修正**: `deploy.yml`の`docker pull`の直後に`docker tag ghcr.io/<owner>/edinet-dl:latest
edinet-dl:latest`を追加し、pull後に必ずローカルタグを付け替えるようにした。

## 検討したが不採用: Tapoのローカル電源制御APIとの連携（2026-08-27）

ジョブ完了直前にTapo（スマートプラグ）のローカルAPIを呼び、数分後に電源を切るよう動的に
指示する案を検討したが、不採用とした。

**技術的な制約**: TP-Link独自プロトコル（KLAP等）を扱うサードパーティ製ライブラリが必要
（stdlibでは実装できない）。ローカル制御であってもTapoアカウントの認証情報がハンドシェイク
に必要になることが多い。

**既存方針との不整合**: 新しい依存ライブラリ・新しい秘密情報の両方が増える。これは
「依存ゼロ・管理する秘密情報を増やさない」という一貫した方針（ghcr.io公開化・Slack通知の
`urllib`実装等）や、ログ設計時に「電源スケジュールと無関係な関心事を結合させない」として
`logrotate`のcrontab直列化案を撤回した判断と同じ構造で反する。

**代替案（採用）**: Tapoの電源遮断時刻は固定（06:00 JST）とし、決算集中日等で処理が間に
合わなくても、`fetch_documents.py`側の`DAYS_WINDOW`による遡り窓で翌日以降に自動的に
再開させる。日次の本番ジョブは所要時間が短く予測可能なため、Tapo固定スケジュール＋OS側
`shutdown`という既存の疎結合設計のままで十分と判断した。詳細は
[mac_mini_setup_runbook.md](./mac_mini_setup_runbook.md)の前提条件を参照。

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

## 未設定（実際に動かすために必要）

- **Secrets**: `TS_OAUTH_CLIENT_ID`/`TS_OAUTH_SECRET`（Tailscale管理画面でOAuthクライアントを
  作成）、`SSH_PRIVATE_KEY`（対応する公開鍵をMac Miniの`~/.ssh/authorized_keys`に登録）、
  `MAC_MINI_HOST`/`MAC_MINI_USER`
- **ghcr.ioパッケージの可視性**: `build-push.yml`初回実行後、GitHubリポジトリのPackages設定で
  手動でPublicに切り替える必要がある（`GITHUB_TOKEN`でpushしたパッケージはデフォルト非公開）
- Mac Mini実機での初期セットアップ（[mac_mini_setup_runbook.md](./mac_mini_setup_runbook.md)参照）

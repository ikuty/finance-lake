# Mac Mini初期セットアップ手順（ランブック）

Mac Mini 2012 + Ubuntu 24.04 LTSに、このリポジトリを初めてデプロイする際の手順。1回きり
（あるいは稀な再構築時）の作業であり、対象マシンは1台のみのため、Ansible等の構成管理
ツールは使わずランブックとして手順を記載する（詳細は
[deployment_design.md](./deployment_design.md)参照）。

対話的な作業（Tailscaleログイン等）を含むため、フルスクリプト化はしていない。コマンドは
コピー＆ペーストして実行することを想定している。`<user>`は実際のユーザー名に読み替える。

## 前提条件（この手順の対象外）

- Ubuntu 24.04 LTSのインストール済み。OS用SSD(`/`, 250GB)・ストレージ用HDD(`/home`, 500GB)
  の構成になっていること
- `setpci`による「AC電源復帰時にOS自動起動」の設定・systemd化は完了済み（別途設定済み）
- スマートプラグ（Tapo P110M）による日次電源スケジュールの設定は完了済み（Tapoアプリ側）。
  **電源投入 04:00:00 JST / 電源遮断 06:00:00 JST**（2026-08-27決定）。`edinet-dl.timer`の
  `OnCalendar`（04:01:30 JST、電源投入から90秒マージン）とセットで管理する。遮断までの
  約118分は、実測データ（閑散日5〜15分、決算集中日でも35〜50分程度）に対して十分な余裕を
  持たせたもの。`shutdown -h now`によりジョブ完了後は実質消費電力ゼロになるため、遮断時刻を
  詰める必要は無いという判断。稀に極端な繁忙日でこの時間内に収まらなくても、`DAYS_WINDOW`
  による遡り窓で翌日以降に自動的に再開される（Tapo側のAPI連携等は行わない。理由は
  [deployment_design.md](./deployment_design.md)参照）

## 1. Tailscaleの導入

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

`tailscale up`実行時に表示されるURLをブラウザで開き、認証する（対話的作業）。以降は
Tailscale経由でSSH接続できることを確認する。

## 2. Dockerの導入

Docker公式のaptリポジトリを使う（`curl | sh`のような検証しづらいスクリプトは避ける）。

```
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 確認
sudo docker run --rm hello-world
```

## 3. `/home`配下にディレクトリを作成し、リポジトリを配置

アプリ本体・データとも`/home`配下（HDD側）に統一する方針（CLAUDE.md参照）。

```
mkdir -p /home/<user>/edinet-dl
cd /home/<user>/edinet-dl
git clone <リポジトリのURL> .
mkdir -p data
```

## 4. `.env`の作成

```
cp .env.example .env
# EDINET_API_KEY に取得済みのAPIキーを設定する
# DAYS_WINDOW・REQUEST_DELAY・DATA_DIR・LOG_PATHは既定値のままでよい
```

## 5. Dockerイメージの用意

初回はGitHub Actions（CI/CD）がまだ稼働していないため、ローカルでビルドする。CI/CD構築後の
2回目以降の更新は`deployment_design.md`の手順（`docker pull ghcr.io/...`）に切り替わる。

```
docker build -t edinet-dl:latest -f docker/Dockerfile .
```

## 6. systemd unitの登録

```
sudo cp systemd/edinet-dl.service systemd/edinet-dl.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now edinet-dl.timer
```

`edinet-dl.service`・`edinet-dl.timer`内のパス（`/home/<user>/edinet-dl/...`）を、
実際のユーザー名に合わせて事前に書き換えておくこと。

## 7. 動作確認

```
systemctl list-timers edinet-dl.timer   # 次回実行予定時刻を確認
sudo systemctl start edinet-dl.service  # 手動で1回実行してみる
journalctl -u edinet-dl.service -f      # ログを確認（アプリ自体のログは/data/logs/edinet-dl.log）
```

## 8. 初回バックフィル（手動・1回限り）

systemdタイマーには含めない。過去10年分を対象に手動実行する（README.md参照）。

```
docker run --rm --env-file .env -v "$(pwd)/data:/data" edinet-dl:latest \
  --start-date 2016-08-13 --end-date 2026-08-25
```

3,650日 × リクエスト間隔1.2秒 ≒ 70〜80分。中断してもfetch_progressテーブルにより
未取得日だけ再開されるので、同じコマンドを再実行すれば安全に続きから再開できる。

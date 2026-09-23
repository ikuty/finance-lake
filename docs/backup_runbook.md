# レイク生データのバックアップ手順（ランブック）

`/home`側HDD（`sdb`）はバックアップを持たない設計だった（CLAUDE.md参照）が、
2026-09-23にUSB外付けHDD（500GB）を導入し、レイクの生データを対象に週次の手動バックアップ
運用を開始する。

## 方針

- **対象**: レイクの生データのみ（`finance-dwh`側は生データ+dbtから再生成可能な導出データの
  ため対象外）。
  ```
  /home/<user>/finance-lake/data/edinet-dl
  /home/<user>/finance-lake/data/jpx-daily-pdf-dl
  /home/<user>/finance-lake/data/mufg-corporate-actions
  ```
  2026-09-23時点で合計約66GB（内訳: edinet-dl 32GB、jpx-daily-pdf-dl 34GB、
  mufg-corporate-actions 2.4MB）。500GBのバックアップHDDに対して十分な余裕がある。
- **ツール**: [restic](https://restic.net/)（重複排除ベースの増分バックアップツール）。
  各スナップショットは変化分のみ実容量を消費するが、復元時はスナップショット単独で
  完全な状態を復元できる（従来型の増分バックアップのようにフルバックアップからの
  チェーンを要しない）。
- **運用**: 常時接続ではなく、**週1回程度、手動でバックアップHDDの電源を入れてMacMiniに
  接続**し、その都度コマンドを実行する。systemdタイマーには含めない（対話的な接続作業を
  伴うため）。
- **前提**: 本体HDD（`sdb`）とバックアップHDDが同時に故障することは想定しない。本体HDD
  故障時はディスク交換後バックアップからリストア、バックアップHDD故障時はバックアップ
  ディスクのみ交換する。

## 1. 初回のみ: バックアップHDDのセットアップ

```
sudo apt install -y restic

# デバイス名を確認（USB接続時のみ表示される。TRAN列がusbのものが対象）
lsblk -d -o NAME,SIZE,MODEL,TRAN

# パーティションのUUIDを確認する（デバイス名はUSB接続順で変わりうるため、
# fstab・マウントにはUUIDを使う）
sudo blkid /dev/sdX1

sudo mkdir -p /mnt/backup
sudo mount UUID=<確認したUUID> /mnt/backup
```

resticリポジトリを初期化する。設定するパスワードはリポジトリの暗号鍵そのものであり、
紛失するとバックアップ全体が復元不能になる。**バックアップHDD自体とは別の場所**
（パスワードマネージャ等）に保管すること。

```
restic -r /mnt/backup/restic-repo init
```

毎回のパスワード入力を省略したい場合は、パスワードファイルを作成し
（リポジトリ管理外の場所に置くこと。`chmod 600`）、以降のコマンドで
`--password-file <path>`を指定する。

## 2. 週次運用（手動）

1. **接続**: バックアップHDDの電源を入れ、MacMiniにUSB接続する
2. **マウント**:
   ```
   sudo mount UUID=<UUID> /mnt/backup
   ```
3. **バックアップ実行**:
   ```
   restic -r /mnt/backup/restic-repo --password-file <path> backup \
     /home/<user>/finance-lake/data/edinet-dl \
     /home/<user>/finance-lake/data/jpx-daily-pdf-dl \
     /home/<user>/finance-lake/data/mufg-corporate-actions
   ```
4. **世代整理**（ディスク容量を無限に消費しないよう間引く。直近8週分は週次スナップショット
   のまま残し、それより古いものは月次1点のみ残す例）:
   ```
   restic -r /mnt/backup/restic-repo --password-file <path> forget \
     --keep-weekly 8 --keep-monthly 6 --prune
   ```
5. **アンマウント・取り外し**:
   ```
   sudo umount /mnt/backup
   ```
   その後、バックアップHDDの電源を切って取り外す。

## 3. 定期メンテナンス（週次より低頻度でよい）

- **整合性チェック**（月1回程度）:
  ```
  restic -r /mnt/backup/restic-repo --password-file <path> check
  ```
- **リストア手順の確認**（年1回程度の演習を推奨。本体HDD故障時に手順で詰まらないため）:
  ```
  restic -r /mnt/backup/restic-repo --password-file <path> snapshots
  restic -r /mnt/backup/restic-repo --password-file <path> restore <snapshot-id> --target <復元先>
  ```

## 4. 本体HDD故障時の復旧手順

1. 故障した`/home`用HDD（`sdb`）を交換する
2. `mac_mini_setup_runbook.md`の手順1〜2（Tailscale・Docker導入）を実施
3. バックアップHDDを接続し、上記手順でマウント
4. `restic restore latest --target /home/<user>/finance-lake/data/` で最新スナップショット
   から生データを復元
5. `mac_mini_setup_runbook.md`の手順3以降（リポジトリ配置・systemd登録等）を実施し、
   通常運用に復帰する

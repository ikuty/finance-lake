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
- **実行はtmuxセッション内で行う**（2026-09-23の実機検証で判明。詳細は「実機検証で判明した
  注意点」参照）。resticコマンドを`tmux`セッション外の素のSSHセッションで直接実行すると、
  SSH接続が切れた時点（ローカル端末を閉じる・スリープする等）でプロセスごと終了してしまう。

## 1. 初回のみ: バックアップHDDのセットアップ

```
sudo apt install -y restic tmux

# デバイス名を確認（USB接続時のみ表示される。TRAN列がusbのものが対象）
lsblk -d -o NAME,SIZE,MODEL,TRAN

# パーティションのUUIDを確認する（デバイス名はUSB接続順で変わりうるため、
# fstab・マウントにはUUIDを使う）
sudo blkid /dev/sdX1

sudo mkdir -p /mnt/backup
sudo mount UUID=<確認したUUID> /mnt/backup

# マウント直後はroot所有のため、以降sudoなしで書き込めるよう所有者を変更する
# (ext4の所有権はディスク側に永続化されるため、この変更は初回のみでよい)
sudo chown "$(whoami):$(whoami)" /mnt/backup
```

パスワードファイルを作成する（リポジトリ管理外の場所に置くこと。`chmod 600`）。中身が
シェル履歴等に残らないよう、非表示入力で設定する。

```
read -s -p "Password: " P && echo "$P" > ~/.restic-password && chmod 600 ~/.restic-password && unset P
```

このファイルの中身がリポジトリの暗号鍵そのものであり、紛失するとバックアップ全体が
復元不能になる。**バックアップHDD自体とは別の場所**（パスワードマネージャ等）に中身を
控えておくこと。

resticリポジトリを初期化する。

```
restic -r /mnt/backup/restic-repo --password-file ~/.restic-password init
```

## 2. 週次運用（手動）

1. **接続**: バックアップHDDの電源を入れ、MacMiniにUSB接続する
2. **マウント**:
   ```
   sudo mount UUID=<UUID> /mnt/backup
   ```
3. **tmuxセッションを開始**（既存セッションがあれば`tmux attach -t backup`で再接続）:
   ```
   tmux new -s backup
   ```
4. **バックアップ実行**（tmuxセッション内で、対象ディレクトリごとに個別のコマンドとして
   実行する。理由は「実機検証で判明した注意点」参照）:
   ```
   restic -r /mnt/backup/restic-repo --password-file ~/.restic-password backup /home/<user>/finance-lake/data/edinet-dl
   restic -r /mnt/backup/restic-repo --password-file ~/.restic-password backup /home/<user>/finance-lake/data/jpx-daily-pdf-dl
   restic -r /mnt/backup/restic-repo --password-file ~/.restic-password backup /home/<user>/finance-lake/data/mufg-corporate-actions
   ```
   完了を待たずにローカル端末を離れたい場合は`Ctrl+B`の後`D`でデタッチする（バックアップは
   継続する）。再接続は`ssh macmini`後に`tmux attach -t backup`。
5. **世代整理**（ディスク容量を無限に消費しないよう間引く。直近8週分は週次スナップショット
   のまま残し、それより古いものは月次1点のみ残す例）:
   ```
   restic -r /mnt/backup/restic-repo --password-file ~/.restic-password forget --keep-weekly 8 --keep-monthly 6 --prune
   ```
6. **アンマウント・取り外し**:
   ```
   sudo umount /mnt/backup
   ```
   その後、バックアップHDDの電源を切って取り外す。

## 3. 定期メンテナンス（週次より低頻度でよい）

- **整合性チェック**（月1回程度）:
  ```
  restic -r /mnt/backup/restic-repo --password-file ~/.restic-password check
  ```
- **リストア手順の確認**（年1回程度の演習を推奨。本体HDD故障時に手順で詰まらないため）:
  ```
  restic -r /mnt/backup/restic-repo --password-file ~/.restic-password snapshots
  restic -r /mnt/backup/restic-repo --password-file ~/.restic-password restore <snapshot-id> --target <復元先>
  ```

## 4. 本体HDD故障時の復旧手順

1. 故障した`/home`用HDD（`sdb`）を交換する
2. `mac_mini_setup_runbook.md`の手順1〜2（Tailscale・Docker導入）を実施
3. バックアップHDDを接続し、上記手順でマウント
4. `restic restore latest --target /home/<user>/finance-lake/data/` で最新スナップショット
   から生データを復元
5. `mac_mini_setup_runbook.md`の手順3以降（リポジトリ配置・systemd登録等）を実施し、
   通常運用に復帰する

## 実機検証で判明した注意点（2026-09-23）

- **USBブリッジチップ（JMicron JMS578）とUASドライバの相性問題**: 初回の`mkfs.ext4`実行中
  （ジャーナル作成の大きな連続書き込み時）に、`uas`（USB Attached SCSI）ドライバでコマンド
  タイムアウト・デバイスのオフライン化が発生し、フォーマットが失敗した
  （`dmesg`に`uas_eh_abort_handler`・`Device offlined - not ready after error recovery`）。
  `uas`モジュールを`blacklist`して`usb-storage`（Bulk-Only Transport）へのフォールバックを
  試みたが、今度はudevが当該デバイスを`usb-storage`にバインドせず（原因不明）認識不能に
  なった。最終的に`uas`のblacklistを解除（元の状態に戻す）して再実行したところ、
  `mkfs.ext4`・5GB連続書き込み(`dd`)・66GB規模のresticバックアップいずれも問題なく完走した
  ため、**最初の失敗は一過性の問題だった**と判断している。もし今後同様の切断症状
  （`journalctl -k`に`uas_eh_abort_handler`等が出て`lsblk`からデバイスが消える）が再発する
  場合は、USBケーブルの抜き差し・接続ポートの変更を先に試すこと。`uas`のblacklistは
  再現性のある解決策ではなかったため、この手順としては採用していない。
- **マウント直後の書き込み権限**: `mount`直後は所有者が`root:root`になり、一般ユーザーでの
  書き込みは`Permission denied`になる。`chown`が必要（上記「初回のみ」手順に反映済み、
  ext4では所有権がディスク側に永続化されるため初回のみでよい）。
- **HDD＋多数の小ファイルはresticでかなり遅くなる**: `edinet-dl`はドキュメント単位でXBRLを
  展開・個別gzip圧縮しているため、平均ファイルサイズが約65KB（実測: 20GiB/31万ファイル）と
  非常に小さい。resticは全ファイルの読み込み・チャンク化・ハッシュ計算が必要なため、
  同じ総容量でもファイル数が桁違いに少ない`jpx-daily-pdf-dl`（日次PDF、1ファイルが数百KB〜
  数MB）と比べて体感速度が大きく劣る。初回バックアップの大半の時間は`edinet-dl`が占める
  ことを見込んでおくこと。
- **対象ディレクトリごとに個別の`backup`コマンドにする理由**: 1つの`backup`コマンドに
  複数パスをまとめて渡すと、スナップショットはコマンド全体が完走して初めて1つ確定する。
  未完走のまま中断すると次回実行時に**全パスを最初から再走査**することになる（既に
  アップロード済みのデータチャンクは重複排除されるため再アップロードは発生しないが、
  読み込み・ハッシュ化の時間は無駄になる）。ディレクトリ単位で分けておけば、完了済みの
  ディレクトリは次回実行時に再走査されない。

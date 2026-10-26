#!/bin/sh
# Mac Mini上のtailscaledを最新版へ更新し、結果をSlackへ通知する。
#
# 背景（2026-10-26）: tailscaled自身のAutoUpdate設定（Check/Apply=true）も、
# Ubuntu側のunattended-upgradesも、Mac Miniの稼働時間が短い（Tapoで1日約2時間）
# ため実際には完走せず、バージョンが何世代も古いまま放置されていたことが判明した
# （journalctlで"offline auto-update: starting/stopping update checks"が同一秒で
# 完了していることを確認）。電源投入直後にこのスクリプトを明示的に実行し、更新を
# 確定させる。
#
# rootで実行する必要があるが、tailscale-update.service（User=未指定）はsystemdの
# system unitとしてデフォルトでrootで実行されるため、対話的なsudoパスワード入力は
# 不要（finance-lake-shutdown.serviceの`shutdown -h now`と同じ仕組み）。

set -eu

BEFORE=$(tailscale version | head -1)

if tailscale update --yes > /tmp/tailscale-update.log 2>&1; then
    AFTER=$(tailscale version | head -1)
    if [ "$BEFORE" = "$AFTER" ]; then
        MSG="tailscale update: 変更なし（${AFTER}）"
    else
        MSG="tailscale update: ${BEFORE} → ${AFTER} に更新しました"
    fi
else
    MSG="tailscale update が失敗しました。macminiの /tmp/tailscale-update.log を確認してください。"
fi

if [ -n "${SLACK_WEBHOOK_URL:-}" ]; then
    ESCAPED=$(printf '%s' "$MSG" | sed 's/\\/\\\\/g; s/"/\\"/g')
    curl -fsS -X POST -H 'Content-type: application/json' \
        --data "{\"text\": \"${ESCAPED}\"}" \
        "$SLACK_WEBHOOK_URL" > /dev/null 2>&1 || true
fi

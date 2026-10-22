#!/bin/bash
# 安装自建更新源的定时同步（服务器端执行一次；tools/sync-updates.py --install 会自动上传并执行本脚本）。
#
# 安装后服务器每 5 分钟用 sync-updates.sh --if-changed 比对 GitHub 最新正式版：
# 版本没变只取一个 1KB 的 latest.yml 就退出；有新版本就直接从 GitHub 下载、校验、部署。
# 发版因此只需推送 tag，GitHub Actions 打包发布后自建源自动跟上，本机不用再做任何操作。
#
# 用法: bash server-install-updater.sh <server-sync-updates.sh 的路径>
# 查看: systemctl list-timers chatcoder-updates.timer ；日志：journalctl -u chatcoder-updates -n 50
# 卸载: systemctl disable --now chatcoder-updates.timer（cron 环境删除 /etc/cron.d/chatcoder-updates）
set -euo pipefail

SRC="${1:?用法: server-install-updater.sh <server-sync-updates.sh 的路径>}"
DIR="${INSTALL_DIR:-/opt/chatcoder-updates}"
SCRIPT="$DIR/sync-updates.sh"

install -d "$DIR"
# 统一 LF：脚本从 Windows 上传时可能带 CRLF，bash 会把 \r 当成命令的一部分
tr -d '\r' < "$SRC" > "$SCRIPT.tmp"
chmod 755 "$SCRIPT.tmp"
mv -f "$SCRIPT.tmp" "$SCRIPT"
bash -n "$SCRIPT"
echo "[install] 同步脚本 -> $SCRIPT"

if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  cat > /etc/systemd/system/chatcoder-updates.service <<EOF
[Unit]
Description=chatcoder 自建更新源：从 GitHub 拉取最新版本
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$SCRIPT --if-changed
Nice=10
TimeoutStartSec=3600
EOF
  cat > /etc/systemd/system/chatcoder-updates.timer <<EOF
[Unit]
Description=chatcoder 自建更新源定时同步（每 5 分钟）

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF
  rm -f /etc/cron.d/chatcoder-updates
  systemctl daemon-reload
  systemctl enable --now chatcoder-updates.timer
  echo "[install] 已启用 systemd 定时器 chatcoder-updates.timer（每 5 分钟）"
  systemctl list-timers chatcoder-updates.timer --no-pager || true
else
  echo "*/5 * * * * root $SCRIPT --if-changed >> /var/log/chatcoder-updates.log 2>&1" > /etc/cron.d/chatcoder-updates
  chmod 644 /etc/cron.d/chatcoder-updates
  echo "[install] 系统没有 systemd，已写入 cron：/etc/cron.d/chatcoder-updates（日志 /var/log/chatcoder-updates.log）"
fi

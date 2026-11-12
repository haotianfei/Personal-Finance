#!/usr/bin/env bash
# install-backup-timer.sh —— 生成并安装宿主备份的 systemd **user** 定时器（person-fin-backup）
#
# 用法：
#   bash deploy/scripts/install-backup-timer.sh            # ＝--dry-run：只打印 unit 内容与将执行的命令
#   bash deploy/scripts/install-backup-timer.sh --apply    # 写 unit 文件到 ~/.config/systemd/user/ + daemon-reload
#   bash deploy/scripts/install-backup-timer.sh --apply --enable   # 再 enable --now（默认不 enable）
#   bash deploy/scripts/install-backup-timer.sh -h
#
# 可选参数：
#   --unit-dir DIR   写到别的目录（默认 ${XDG_CONFIG_HOME:-~/.config}/systemd/user）。
#                    自定义目录**不会**触发 daemon-reload —— systemd 根本看不到那里，写文件只为审阅/测试。
#   --time HH:MM     OnCalendar 的时间（默认 03:00）
#
# 为什么是 user unit 而不是 system unit：
#   * 部署目录在用户家目录/共享卷下，归用户所有，root 备份会把新 tar 的属主写成 root；
#   * user unit 不需要 sudo，升级/搬迁部署目录时改一行路径即可。
#   代价：用户没有活动 session 时 user manager 可能不跑 —— 需要常驻就 `loginctl enable-linger <user>`
#   （本脚本**不改** linger，只打印命令）。
#
# 生成的 unit 内容随脚本走、**不入库**（路径是本机绝对路径）。
# 卸载：systemctl --user disable --now person-fin-backup.timer && rm -f <两个 unit 文件> && systemctl --user daemon-reload
#
# 退出码：0 正常 / 1 写文件或 systemctl 失败 / 2 环境缺失
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DEPLOY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_SCRIPT="$DEPLOY_DIR/scripts/backup-data.sh"

UNIT_NAME="person-fin-backup"
SERVICE="$UNIT_NAME.service"
TIMER="$UNIT_NAME.timer"

DEFAULT_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DIR="$DEFAULT_UNIT_DIR"
ON_CALENDAR_TIME="03:00"

APPLY=0
ENABLE=0

die()   { echo "[FAIL] $*" >&2; exit 1; }
env_die() { echo "[环境缺失] $*" >&2; exit 2; }
note()  { echo; echo "==> $*"; }
sub()   { echo "    $*"; }
usage() { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)    APPLY=1; shift ;;
    --dry-run)  APPLY=0; shift ;;
    --enable)   ENABLE=1; shift ;;
    --unit-dir) UNIT_DIR="${2:?--unit-dir 需要目录}"; shift 2 ;;
    --time)     ON_CALENDAR_TIME="${2:?--time 需要 HH:MM}"; shift 2 ;;
    -h|--help)  usage; exit 0 ;;
    *) die "未知参数：$1（-h 看用法）" ;;
  esac
done

[[ "$ON_CALENDAR_TIME" =~ ^[0-2][0-9]:[0-5][0-9]$ ]] \
  || die "--time 需要 HH:MM（收到 $ON_CALENDAR_TIME）"
[[ -f "$BACKUP_SCRIPT" ]] || env_die "找不到备份脚本：$BACKUP_SCRIPT"

service_unit() {
  cat <<EOF
[Unit]
Description=person_fin 宿主数据目录备份（deploy/data → deploy/backups，含保留策略）
Documentation=file://$BACKUP_SCRIPT

[Service]
Type=oneshot
WorkingDirectory=$DEPLOY_DIR
ExecStart=$BACKUP_SCRIPT --apply
# 300MB 级 tar 可能超过 systemd 对 oneshot 的默认 start 超时（90s），显式放宽
TimeoutStartSec=3600
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=6
EOF
}

timer_unit() {
  cat <<EOF
[Unit]
Description=person_fin 每日 $ON_CALENDAR_TIME 宿主备份（Persistent：错过则下次开机补跑）

[Timer]
OnCalendar=*-*-* $ON_CALENDAR_TIME:00
Persistent=true
AccuracySec=1min
Unit=$SERVICE

[Install]
WantedBy=timers.target
EOF
}

note "目标（$([[ "$APPLY" -eq 1 ]] && echo '--apply：写文件' || echo 'dry-run：只打印')）"
sub "部署目录   : $DEPLOY_DIR"
sub "备份脚本   : $BACKUP_SCRIPT"
sub "unit 目录  : $UNIT_DIR$([[ "$UNIT_DIR" != "$DEFAULT_UNIT_DIR" ]] && echo '（自定义目录，不触发 daemon-reload）')"
sub "定时       : 每日 $ON_CALENDAR_TIME（Persistent=true）"
sub "enable     : $([[ "$ENABLE" -eq 1 ]] && echo '是（enable --now）' || echo '否（默认只落文件）')"

echo
echo "---------------- $UNIT_DIR/$SERVICE ----------------"
service_unit
echo "---------------- $UNIT_DIR/$TIMER ----------------"
timer_unit

note "将执行的命令"
sub "1) mkdir -p $UNIT_DIR"
sub "2) 写 $UNIT_DIR/$SERVICE（覆盖）"
sub "3) 写 $UNIT_DIR/$TIMER（覆盖）"
if [[ "$UNIT_DIR" == "$DEFAULT_UNIT_DIR" ]]; then
  sub "4) systemctl --user daemon-reload"
else
  sub "4) （自定义 --unit-dir，不跑 daemon-reload）"
fi
if [[ "$ENABLE" -eq 1 ]]; then
  sub "5) systemctl --user enable --now $TIMER"
  sub "6) systemctl --user list-timers $TIMER"
else
  sub "5) （未 --enable：不 enable --now，只落文件）"
fi
if [[ "$APPLY" -ne 1 ]]; then
  sub "（dry-run 预览：以上命令一条都没执行）"
fi

note "装好之后建议手工确认（本脚本不代跑）"
sub "systemctl --user list-timers $TIMER"
sub "systemctl --user start $SERVICE          # 手工触发一次，应产出一份新 tar"
sub "journalctl --user -u $SERVICE -n 50"
sub "loginctl enable-linger \$USER             # 只在「没登录也想跑」时需要（会改 linger 策略，自行确认）"

if [[ "$APPLY" -ne 1 ]]; then
  echo
  echo "==> dry-run 结束：未创建/未修改任何文件，未调用 systemctl。加 --apply 落盘。"
  exit 0
fi

# ------------------------------------------------------------------ 落盘
mkdir -p "$UNIT_DIR"
service_unit > "$UNIT_DIR/$SERVICE.tmp"
timer_unit   > "$UNIT_DIR/$TIMER.tmp"
mv -f "$UNIT_DIR/$SERVICE.tmp" "$UNIT_DIR/$SERVICE"
mv -f "$UNIT_DIR/$TIMER.tmp"   "$UNIT_DIR/$TIMER"
note "已写入"
sub "$UNIT_DIR/$SERVICE"
sub "$UNIT_DIR/$TIMER"

if [[ "$UNIT_DIR" == "$DEFAULT_UNIT_DIR" ]]; then
  if command -v systemctl >/dev/null 2>&1; then
    note "systemctl --user daemon-reload"
    systemctl --user daemon-reload || die "daemon-reload 失败（user manager 是否可用？）"
    sub "ok"
  else
    echo "[warn] 没有 systemctl：unit 已落盘但未被 systemd 载入" >&2
  fi
else
  echo "[note] 自定义 --unit-dir：跳过 daemon-reload（systemd 不读该目录）"
fi

if [[ "$ENABLE" -eq 1 ]]; then
  note "systemctl --user enable --now $TIMER"
  systemctl --user enable --now "$TIMER" || die "enable --now 失败"
  systemctl --user list-timers "$TIMER" || true
else
  note "未 enable（默认）。要定时生效再跑："
  sub "systemctl --user enable --now $TIMER"
fi

echo
echo "==> 完成"
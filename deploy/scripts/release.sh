#!/usr/bin/env bash
# release.sh —— 生产上线门禁：7 步，默认 dry-run
#
# 七步（任一失败即停；第 7 步负责兜底）：
#   ① 门禁    bash scripts/ci.sh（唯一 CI 入口；--skip-ci 仅限紧急，RELEASES 留痕 ci=skipped）
#   ② 回滚点  现役 :local → :rollback-<YYYYMMDD-HHMMSS>（前后端各一个）
#   ③ 构建    package.sh + docker compose build，产出 :local 与 :<YYYYMMDD>-<shortsha>
#   ④ 上线    docker compose --env-file .env up -d（**仅 --apply 才执行**）
#   ⑤ 验收    等前后端 healthy + deploy/scripts/smoke.sh（6 条 200）
#   ⑥ 留痕    追加 deploy/RELEASES.md 一行（时间 / commit / 版本 tag / smoke / 备注）
#   ⑦ 兜底    已 up -d 后失败 → 自动 rollback.sh <回滚点>；该行标 FAIL
#
# 退出码（与 scripts/ci.sh 的约定一致）：0 通过 / 1 断言失败 / 2 环境缺失
#
# 用法：
#   bash deploy/scripts/release.sh                     # dry-run：打印 7 步与将执行的命令，不改任何状态
#   bash deploy/scripts/release.sh --apply             # 真上线
#   bash deploy/scripts/release.sh --apply --skip-ci   # 紧急跳过 CI（RELEASES 会记 ci=skipped）
#   bash deploy/scripts/release.sh -h                  # 本帮助
#
# 环境变量（隔离/演练用，正常发布不需要）：
#   REPO_DIR              仓库根（默认 deploy 的上一级；package.sh 认同一变量）
#   DEPLOY_DIR_OVERRIDE   覆盖部署目录（演练 .env/build 缺失等错误分支时用）
#   RELEASE_OPERATOR      RELEASES 备注里的操作者（默认当前用户名）
#
# 安全边界：
#   * 只操作生产项目 person_fin（compose 文件 deploy/docker-compose.yml，镜像 person_fin-{backend,frontend}）；
#   * 不碰 deploy/docker-compose.staging.yml、staging 项目与 staging-* 镜像；
#   * dry-run 不做任何写操作（docker tag/build/up 全部只打印）。
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
DEPLOY_DIR="${DEPLOY_DIR_OVERRIDE:-$(cd "$(dirname "$0")/.." && pwd)}"
REPO_DIR="${REPO_DIR:-$(cd "$DEPLOY_DIR/.." && pwd)}"
export REPO_DIR                      # package.sh 用 ${REPO_DIR:-...}
cd "$DEPLOY_DIR"

BE_REPO="person_fin-backend"
FE_REPO="person_fin-frontend"
REL_FILE="$DEPLOY_DIR/RELEASES.md"
ENV_FILE="$DEPLOY_DIR/.env"

APPLY=0
SKIP_CI=0

die()     { echo "[FAIL] $*" >&2; exit 1; }
env_die() { echo "[环境缺失] $*" >&2; exit 2; }
note()    { echo; echo "==> $*"; }
sub()     { echo "    $*"; }

usage() { awk 'NR>1 && /^#/ {sub(/^# ?/,""); print; next} NR>1 {exit}' "$SELF"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)   APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --skip-ci) SKIP_CI=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1（-h 看用法）" ;;
  esac
done

# ---------------------------------------------------------------- 基础量

START_TS="$(date '+%Y-%m-%d %H:%M:%S')"
STAMP="$(date '+%Y%m%d-%H%M%S')"     # 回滚点时间戳
TODAY="$(date '+%Y%m%d')"            # 版本 tag 日期
OPERATOR="${RELEASE_OPERATOR:-$(id -un)}"

SHORTSHA=""
if ! SHORTSHA="$(git -c safe.directory='*' -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null)"; then
  SHORTSHA=""
fi
VER="${TODAY}-${SHORTSHA:-<shortsha>}"
RB_TAG="rollback-${STAMP}"

DIRTY=0
if [[ -n "$(git -c safe.directory='*' -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]]; then DIRTY=1; fi

# 运行期状态（供第 7 步判断要不要回滚）
DK=()
CI_NOTE="ci=pending"
SMOKE_RESULT="未跑"
RB_AVAILABLE=0
TAG_MOVED=0
DEPLOYED=0
REL_LINE_NO=""

# ---------------------------------------------------------------- 通用执行器

run() {           # 会改状态的命令：只有 --apply 才真执行
  if [[ "$APPLY" -eq 1 ]]; then
    printf '    $ %s\n' "$*"
    "$@"
  else
    printf '    [dry-run] %s\n' "$*"
    return 0
  fi
}

# ---------------------------------------------------------------- RELEASES

releases_ensure() {
  [[ -f "$REL_FILE" ]] && return 0
  cat > "$REL_FILE" <<'HDR'
# RELEASES · 生产上线与回滚台账（append-only）

由 `deploy/scripts/release.sh` 与 `deploy/scripts/rollback.sh` 自动追加。
**不要改历史行**：记录与实际不符时补一行说明（带「修正」字样），而不是覆盖。

| 时间 | commit | 版本 tag | smoke | 备注 |
| --- | --- | --- | --- | --- |
HDR
}

releases_append() {
  releases_ensure
  printf '%s\n' "$1" >> "$REL_FILE"
  REL_LINE_NO="$(awk 'END{print NR}' "$REL_FILE")"
}

releases_record() {          # 已有占位行则就地替换，否则追加
  if [[ -n "$REL_LINE_NO" && -f "$REL_FILE" ]]; then
    local last; last="$(awk 'END{print NR}' "$REL_FILE")"
    if [[ "$last" == "$REL_LINE_NO" ]]; then
      local tmp; tmp="$(mktemp "${TMPDIR:-/tmp}/releases.XXXXXX")"
      awk -v n="$REL_LINE_NO" -v t="$1" 'NR==n{print t; next}{print}' "$REL_FILE" > "$tmp"
      cat "$tmp" > "$REL_FILE"   # 保留 inode/权限（部署目录在同步盘上，别 mv）
      rm -f "$tmp"
      return 0
    fi
  fi
  releases_append "$1"
}

# ---------------------------------------------------------------- 预检

PREFLIGHT_ERRORS=()
pf_err() { PREFLIGHT_ERRORS+=("$*"); }

preflight() {
  if [[ ! -f "$ENV_FILE" ]]; then
    pf_err "缺少 $ENV_FILE；先 cp deploy/.env.example deploy/.env（端口/数据目录都在里面）"
  fi
  if [[ ! -d "$DEPLOY_DIR/build/backend" || ! -d "$DEPLOY_DIR/build/frontend" ]]; then
    pf_err "构建上下文缺失（$DEPLOY_DIR/build/{backend,frontend}）；先 bash deploy/scripts/package.sh"
  fi
  if [[ "$SKIP_CI" -eq 0 && ! -f "$REPO_DIR/scripts/ci.sh" ]]; then
    pf_err "CI 入口缺失：$REPO_DIR/scripts/ci.sh（票 05 未落地时只能用 --skip-ci，RELEASES 会记 ci=skipped）"
  fi
  if [[ -z "$SHORTSHA" ]]; then
    pf_err "取不到 git commit（git -c safe.directory='*' -C $REPO_DIR rev-parse --short HEAD 失败），无法生成 <YYYYMMDD>-<shortsha> 版本 tag"
  fi
  # 编排契约：脚本的 tag 假设必须与 compose 里的 image 名一致，不一致就拒绝继续
  grep -qE "^[[:space:]]*image:[[:space:]]*${BE_REPO}:local[[:space:]]*$" "$DEPLOY_DIR/docker-compose.yml" 2>/dev/null \
    || pf_err "deploy/docker-compose.yml 的 backend image 不是 ${BE_REPO}:local（tag 契约失效，拒绝继续）"
  grep -qE "^[[:space:]]*image:[[:space:]]*${FE_REPO}:local[[:space:]]*$" "$DEPLOY_DIR/docker-compose.yml" 2>/dev/null \
    || pf_err "deploy/docker-compose.yml 的 frontend image 不是 ${FE_REPO}:local（tag 契约失效，拒绝继续）"

  if docker info >/dev/null 2>&1; then
    DK=(docker)
  elif sudo -n docker info >/dev/null 2>&1; then
    DK=(sudo -n docker)      # sudo 会清环境；本脚本不靠环境传参（镜像名写死、.env 走 --env-file）
  else
    pf_err "docker 不可用（当前用户无权限且 sudo -n 不通）"
  fi
}

show_plan() {
  echo
  echo "release.sh · $([[ "$APPLY" -eq 1 ]] && echo 'APPLY（真上线）' || echo 'dry-run（不改状态）')"
  sub "部署目录 : $DEPLOY_DIR"
  sub "仓库根   : $REPO_DIR"
  sub "commit   : ${SHORTSHA:-<未知>}$([[ "$DIRTY" -eq 1 ]] && echo '（工作区有未提交改动）')"
  sub "版本 tag : $VER   （同时保留 :local）"
  sub "回滚点   : $RB_TAG   （第 ② 步给现役 :local 打的快照）"
  sub "操作者   : $OPERATOR"
  sub "CI       : $([[ "$SKIP_CI" -eq 1 ]] && echo '--skip-ci（紧急跳过，会留痕）' || echo 'scripts/ci.sh')"
}

# ---------------------------------------------------------------- 步骤实现

step1_ci() {
  note "① CI 门禁（唯一入口：scripts/ci.sh）"
  if [[ "$SKIP_CI" -eq 1 ]]; then
    sub "[skip] --skip-ci：跳过 CI（仅限紧急；RELEASES 备注记 ci=skipped）"
    CI_NOTE="ci=skipped"
    return 0
  fi
  if [[ "$APPLY" -ne 1 ]]; then
    sub "[dry-run] (cd $REPO_DIR && bash scripts/ci.sh)"
    return 0
  fi
  sub "\$ (cd $REPO_DIR && bash scripts/ci.sh)"
  local rc=0
  ( cd "$REPO_DIR" && bash scripts/ci.sh ) || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    CI_NOTE="ci=pass"
    sub "CI 通过"
    return 0
  fi
  CI_NOTE="ci=fail(rc=$rc)"
  echo "[FAIL] CI 未通过（rc=$rc）：修复后再发布；紧急放行用 --skip-ci（会留痕）" >&2
  return "$rc"
}

step2_rollback_point() {
  note "② 回滚点：现役 :local → :$RB_TAG"
  local be="$BE_REPO:local" fe="$FE_REPO:local"
  if ! "${DK[@]}" image inspect "$be" >/dev/null 2>&1 || ! "${DK[@]}" image inspect "$fe" >/dev/null 2>&1; then
    RB_AVAILABLE=0
    echo "    [warn] 现役 :local 不全（前后端之一不存在）→ 视为首次部署，无回滚点" >&2
    echo "           本次若失败将没有可回退的镜像，请先确认这是第一次上线" >&2
    return 0
  fi
  RB_AVAILABLE=1
  run "${DK[@]}" tag "$be" "$BE_REPO:$RB_TAG" || return 1
  run "${DK[@]}" tag "$fe" "$FE_REPO:$RB_TAG" || return 1
  if [[ "$APPLY" -eq 1 ]]; then
    sub "回滚点已就位：$BE_REPO:$RB_TAG / $FE_REPO:$RB_TAG"
  fi
}

step3_build() {
  note "③ 构建并打版本 tag：:$VER（同时产出 :local）"
  sub "3.1 暂存构建上下文（package.sh）"
  run bash "$DEPLOY_DIR/scripts/package.sh" || return 1
  sub "3.2 compose build"
  run "${DK[@]}" compose --env-file .env build || return 1
  sub "3.3 版本 tag（:local 已由 build 产出，这里补一个不可变 tag）"
  run "${DK[@]}" tag "$BE_REPO:local" "$BE_REPO:$VER" || return 1
  run "${DK[@]}" tag "$FE_REPO:local" "$FE_REPO:$VER" || return 1
  if [[ "$APPLY" -eq 1 ]]; then
    TAG_MOVED=1
    "${DK[@]}" images --format '{{.Repository}}:{{.Tag}}  {{.ID}}' \
      | grep -E "^(${BE_REPO}|${FE_REPO}):(local|${VER}) " | sed 's/^/    /' || true
  fi
}

step4_deploy() {
  note "④ 上线：docker compose --env-file .env up -d（仅 --apply 执行）"
  if [[ "$APPLY" -ne 1 ]]; then
    sub "[dry-run] ${DK[*]:-docker} compose --env-file .env up -d"
    sub "本模式不执行：dry-run 不改任何容器/镜像状态"
    return 0
  fi
  # 先落一行占位（进程被强杀也留痕），⑥/⑦ 再就地改写
  releases_append "| $START_TS | ${SHORTSHA:-?} | $VER | running | $OPERATOR · 上线中（占位行，未收尾=被打断） |"
  # 一旦开始 up -d 就按「可能需要回滚」对待：up 中途失败时可能已有容器被重建，
  # 只有回滚（重打 :local + 再 up -d）才能保证前后端回到同一版
  DEPLOYED=1
  run "${DK[@]}" compose --env-file .env up -d || return 1
}

step5_verify() {
  note "⑤ 验收：等 healthy + smoke.sh"
  if [[ "$APPLY" -ne 1 ]]; then
    sub "[dry-run] 轮询 ${DK[*]:-docker} inspect --format '{{.State.Health.Status}}' <前后端 cid>（最多 90×5s）"
    sub "[dry-run] 核对运行中镜像 == 刚构建的 :local"
    sub "[dry-run] bash $DEPLOY_DIR/scripts/smoke.sh"
    return 0
  fi
  wait_healthy backend 90 || { SMOKE_RESULT="fail（后端未 healthy）"; return 1; }
  wait_healthy frontend 30 || { SMOKE_RESULT="fail（前端未 healthy）"; return 1; }
  assert_running_image backend "$BE_REPO:local" || { SMOKE_RESULT="fail（后端镜像不是新构建）"; return 1; }
  assert_running_image frontend "$FE_REPO:local" || { SMOKE_RESULT="fail（前端镜像不是新构建）"; return 1; }
  local rc=0
  run bash "$DEPLOY_DIR/scripts/smoke.sh" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    SMOKE_RESULT="6/6 ok"
    return 0
  fi
  SMOKE_RESULT="fail（smoke rc=$rc）"
  echo "[FAIL] smoke 未全绿（rc=$rc）" >&2
  return 1
}

wait_healthy() {                 # $1=service $2=最多轮数（每轮 5s）
  local svc="$1" rounds="$2" cid state="unknown"
  cid="$("${DK[@]}" compose --env-file .env ps -q "$svc")"
  [[ -n "$cid" ]] || { echo "    找不到 $svc 容器" >&2; return 1; }
  for _ in $(seq 1 "$rounds"); do
    state="$("${DK[@]}" inspect --format '{{.State.Health.Status}}' "$cid" 2>/dev/null || echo unknown)"
    case "$state" in
      healthy)   sub "$svc healthy"; return 0 ;;
      unhealthy) echo "    $svc unhealthy（日志：sudo docker compose --env-file .env logs --tail=100 $svc）" >&2; return 1 ;;
      *)         sleep 5 ;;
    esac
  done
  echo "    等 $svc healthy 超时（最后状态：$state）" >&2
  return 1
}

assert_running_image() {         # $1=service $2=期望 tag
  local svc="$1" tag="$2" cid want got
  cid="$("${DK[@]}" compose --env-file .env ps -q "$svc")"
  want="$("${DK[@]}" image inspect --format '{{.Id}}' "$tag" 2>/dev/null || true)"
  got="$("${DK[@]}" inspect --format '{{.Image}}' "$cid" 2>/dev/null || true)"
  if [[ -n "$want" && "$want" == "$got" ]]; then
    sub "$svc 运行镜像 = $tag（${got#sha256:}）"
    return 0
  fi
  echo "    $svc 运行镜像与 $tag 不一致（期望 ${want:-?} 实际 ${got:-?}）" >&2
  return 1
}

finish_ok() {
  note "⑥ 留痕：$REL_FILE"
  local memo="$OPERATOR"
  [[ "$SKIP_CI" -eq 1 ]] && memo="$memo · $CI_NOTE"
  [[ "$DIRTY" -eq 1 ]] && memo="$memo · dirty-tree"
  memo="$memo · local+$VER"
  local row; row="| $START_TS | ${SHORTSHA:-?} | $VER | $SMOKE_RESULT | $memo |"
  releases_record "$row"
  sub "$row"
  note "上线完成"
  sub "版本 tag : $BE_REPO:$VER / $FE_REPO:$VER"
  sub "现役 tag : :local（同镜像）"
  sub "回退     : bash deploy/scripts/rollback.sh $RB_TAG"
}

fail_release() {                 # $1=失败步骤描述 $2=退出码
  local stage="$1" rc="$2"
  echo >&2
  echo "[FAIL] 第 $stage 步失败（rc=$rc）" >&2

  local memo="FAIL · $OPERATOR · $stage 失败"
  [[ "$SKIP_CI" -eq 1 ]] && memo="$memo · $CI_NOTE"

  if [[ "$APPLY" -eq 1 && "$DEPLOYED" -eq 1 ]]; then
    if [[ "$RB_AVAILABLE" -eq 1 ]]; then
      note "⑦ 兜底：自动回滚到 $RB_TAG"
      releases_record "| $START_TS | ${SHORTSHA:-?} | $VER | $SMOKE_RESULT | $memo · 自动回滚中 → 见下一行 |"
      if bash "$DEPLOY_DIR/scripts/rollback.sh" "$RB_TAG"; then
        sub "已自动回滚到 $RB_TAG"
      else
        echo "[FAIL] 自动回滚也失败了：立刻人工介入（bash deploy/scripts/rollback.sh $RB_TAG）" >&2
        rc=1
      fi
    else
      releases_record "| $START_TS | ${SHORTSHA:-?} | $VER | $SMOKE_RESULT | $memo · 无回滚点（首次部署），容器停在新版本，需人工判断 |"
      echo "[FAIL] 没有回滚点（首次部署），容器停在新版本：人工判断" >&2
    fi
  elif [[ "$APPLY" -eq 1 && "$TAG_MOVED" -eq 1 ]]; then
    if [[ "$RB_AVAILABLE" -eq 1 ]]; then
      releases_record "| $START_TS | ${SHORTSHA:-?} | $VER | $SMOKE_RESULT | $memo · 未上线（容器仍是旧版），:local tag 已被构建覆盖，可用 rollback.sh $RB_TAG 复原 |"
      echo "[提示] 未执行 up -d，容器仍跑旧镜像；但 :local tag 已指向新构建：" >&2
      echo "       bash deploy/scripts/rollback.sh $RB_TAG  # 把 :local 指回回滚点" >&2
    else
      releases_record "| $START_TS | ${SHORTSHA:-?} | $VER | $SMOKE_RESULT | $memo · 未上线，:local tag 已被构建覆盖；首次部署无回滚点 |"
      echo "[提示] 未执行 up -d，容器仍跑旧镜像；:local tag 已指向新构建，且没有回滚点" >&2
    fi
  else
    releases_record "| $START_TS | ${SHORTSHA:-?} | $VER | $SMOKE_RESULT | $memo · 未上线、容器未变（镜像 tag 可能已部分生成，用 rollback.sh 无参模式核对） |"
  fi
  exit "$rc"
}

# ---------------------------------------------------------------- 主流程

preflight
show_plan

if [[ "${#PREFLIGHT_ERRORS[@]}" -gt 0 ]]; then
  if [[ "$APPLY" -eq 1 ]]; then
    echo >&2
    echo "[环境缺失] 预检未通过，拒绝上线（退出码 2）：" >&2
    printf '  - %s\n' "${PREFLIGHT_ERRORS[@]}" >&2
    exit 2
  fi
  echo
  echo "[warn] 预检问题（不影响本次 dry-run 演示；--apply 时会以退出码 2 中止）："
  printf '  - %s\n' "${PREFLIGHT_ERRORS[@]}"
fi

CI_RC=0
step1_ci || CI_RC=$?
if [[ "$CI_RC" -ne 0 ]]; then
  # CI 未过 = 断言失败(1) 或 环境缺失(2)，原样透出
  [[ "$CI_RC" -eq 2 ]] || CI_RC=1
  fail_release "① CI 门禁" "$CI_RC"
fi

step2_rollback_point || fail_release "② 回滚点" 1
step3_build          || fail_release "③ 构建"   1
step4_deploy         || fail_release "④ 上线"   1
step5_verify         || fail_release "⑤ 验收"   1

if [[ "$APPLY" -eq 1 ]]; then
  finish_ok
else
  note "⑥ 留痕（dry-run 不写）"
  sub "[dry-run] 将追加 deploy/RELEASES.md：| <时间> | ${SHORTSHA:-<sha>} | $VER | 6/6 ok | $OPERATOR · ci=$([[ "$SKIP_CI" -eq 1 ]] && echo skipped || echo pass) |"
  note "⑦ 兜底（dry-run 不启用）"
  sub "[dry-run] ④/⑤ 失败时自动执行：bash deploy/scripts/rollback.sh $RB_TAG 并把该行标 FAIL"
  echo
  echo "==> dry-run 结束：未执行任何构建、tag、up -d，状态未变"
fi
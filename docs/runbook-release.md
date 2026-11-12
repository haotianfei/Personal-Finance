# 上线 Runbook（发布 / 回滚 / 备份恢复）

**状态**：v1.0（2026-11-09，票 `env-tiers` 03/04/05/06/07）
**适用**：单机 Docker Compose + SQLite 单写者；生产项目名 `person_fin`
**上游**：`.scratch/env-tiers/map.md`（决策 Q1–Q8）、`deploy/docker-compose.yml`、`deploy/RELEASES.md`

> 一句话：**上线只走 `release.sh`，它先过门禁再打回滚点再上线；出问题就 `rollback.sh`，不在生产环境上手改镜像 tag。**

---

## 1. 三个环境

| 环境 | 前端 | 后端 | 数据 | 编排 | 起停入口 |
|---|---|---|---|---|---|
| dev（源码热改） | 6679 | 6678 | `data/person_fin_dev.db`（`db_role=dev`） | 根 `docker-compose.yml`（容器化备选） | `bash scripts/dev-up.sh` / `dev-down.sh` / `dev-status.sh` |
| staging（预发） | 6689 | 6688 | `deploy-staging/data/person_fin_staging.db`（`db_role=staging`，生产库副本） | `deploy/docker-compose.staging.yml`（项目 `person_fin_staging`） | `bash scripts/staging.sh up\|down\|status\|logs` |
| prod（生产） | 6699 | 6698 | `deploy/data/person_fin.db`（`db_role=prod`） | `deploy/docker-compose.yml`（项目 `person_fin`） | `deploy/scripts/release.sh` / `rollback.sh` |

端口规则：后端尾 8 / 前端尾 9，十位数字表环境（7=dev、8=staging、9=prod）。
**2026-11-09 之前**的旧值 6666/6667（落在 WHATWG 屏蔽端口段 6665–6669，Next 拒绝启动）、6798/6799、6788/6789 已作废；生产已于 **2026-11-09** 由 6788/6789 切换到 6698/6699（release `20261109-aa43d66`，smoke 6/6）。

**数据库红线**：`deploy/data/**` 只读。任何造数、演练、探针都用 backup API 副本（`scripts/sync_deploy_db.py` / `scripts/staging-refresh.sh`），禁止裸 `cp`（生产库常带未合并 `-wal`，裸拷会得到陈旧快照）。

**库角色**：由文件名推导（`_dev.db→dev`、`_staging.db→staging`、恰为 `person_fin.db→prod`、其余 `unknown`），`DB_ROLE` 可覆盖。是约定与告警，不是访问控制。启动日志第一行应出现 `database_role … db_path=… db_role=…`。

---

## 2. 日常发布（七步，`release.sh`）

### 2.1 前置

- 工作区干净（`git -c safe.directory=* status --short` 为空）；`deploy/.env` 存在；`deploy/build/` 由 `package.sh` 现做。
- 本地环境齐：`bun`、`.venv`（含 pytest）、`frontend/node_modules`。

### 2.2 预检（不改任何状态）

```bash
bash deploy/scripts/release.sh            # 默认 dry-run：打印 7 步与将执行的命令
```

### 2.3 上线

```bash
bash deploy/scripts/release.sh --apply
```

| 步 | 动作 | 失败时 |
|---|---|---|
| ① 门禁 | `bash scripts/ci.sh`（pytest 全量 + tsc + next lint + doc-driven + public-safety） | 直接停，不动生产；`--skip-ci` 仅限紧急且 `RELEASES.md` 记 `ci=skipped` |
| ② 回滚点 | 现役 `:local` → `:rollback-<YYYYMMDD-HHMMSS>`（前后端各一） | 停（此时生产未变） |
| ③ 构建 | `package.sh` + `docker compose build`，同时打 `:<YYYYMMDD>-<shortsha>` 与 `:local` | 停（生产仍跑旧容器） |
| ④ 上线 | `docker compose --env-file .env up -d`；先写 `RELEASES.md` 占位行 | 进 ⑦ |
| ⑤ 验收 | 等前后端 healthy + `deploy/scripts/smoke.sh`（6 条 200） | 进 ⑦ |
| ⑥ 留痕 | `RELEASES.md` 追加 `时间 / commit / 版本 tag / smoke / 备注` | 进 ⑦ |
| ⑦ 兜底 | 已 `up -d` 后任一步失败 → 自动 `rollback.sh <回滚点>`，该行标 FAIL | — |

预期耗时：门禁约 15 分钟（pytest 全量 800+ 秒是主因），其余 2–4 分钟。急时可先 `bash scripts/ci.sh --fast`（约 1 分钟）自查，但 `release.sh` 仍会跑全量门禁。

### 2.4 发布后核对

```bash
sudo -n docker compose -f deploy/docker-compose.yml -p person_fin --env-file deploy/.env ps
bash deploy/scripts/smoke.sh                       # 生产基址默认 6699/6698
tail -5 deploy/RELEASES.md
sudo -n docker compose -f deploy/docker-compose.yml -p person_fin --env-file deploy/.env logs backend | grep database_role
# 期望 db_role=prod，且 migrations_noop 或 applied 有记录
```

---

## 3. 回滚

```bash
bash deploy/scripts/rollback.sh                     # 无参：列出现役 :local 与可用 rollback-*/版本 tag
bash deploy/scripts/rollback.sh <tag> --dry-run     # 先看动作
bash deploy/scripts/rollback.sh <tag>               # 执行：tag → :local（前后端成对）→ up -d → healthy → smoke → RELEASES 记一行
```

- 回滚点是 `release.sh` 第 ② 步自动打的；**每次上线前一定存在一个可回退点**。
- 数据库回退不在 `rollback.sh` 范围内：镜像回退不动 `deploy/data`；若需连库一起回退，见 §5。
- 镜像清理 `bash deploy/scripts/prune-images.sh [--apply]`：每个仓库保留最近 5 个版本化 tag + 最近 3 个 `rollback-*`，永不动 `:local` / `staging-*` / `pre-*` / `latest` 与被容器引用的镜像。

---

## 4. 生产端口切换窗口（6788/6789 → 6698/6699）——**已于 2026-11-09 执行完毕**

一次性窗口动作，走发布流程做，不用手工 `up -d` 裸跑。

1. 改 `deploy/.env` 两行：`FRONTEND_PORT=6699`、`BACKEND_PORT=6698`（`.env` 不入库；`.env.example` 同步注释）。
2. `bash deploy/scripts/release.sh --apply`（门禁 → 回滚点 → 双 tag → 重建 → smoke）。
3. ~~`smoke.sh` 缺省基址切换后再改~~ → **已完成**：`smoke.sh` 缺省已是 6699/6698，`deploy/README.md`、根 `MEMORY.md` 已同步。
4. 核对：`ss -ltn | grep -E ':(6698|6699)'` 有监听、`:(6788|6789)` 无监听；`bash deploy/scripts/smoke.sh --backend 127.0.0.1:6698 --frontend 127.0.0.1:6699` 6/6 200。
5. 失败回退：改回 `.env` 两行 → `bash deploy/scripts/rollback.sh <刚打的回滚点>`（或重新 `release.sh --apply`）。

---

## 5. 备份与恢复

### 5.1 备份（宿主侧整目录 tar）

```bash
bash deploy/scripts/backup-data.sh             # 备份一份 + 保留策略 dry-run 报告
bash deploy/scripts/backup-data.sh --apply     # 备份 + 按策略真删超期 tar
```

保留策略：日备份留 14 天；每月 1 号那份额外留 6 个月；删除前打印 KEEP/DEL 清单；只认 `data-<TS>.tar.gz`，`orphan-*`、`*.db`、`*.db-wal` 不动。年龄取文件名时间戳（不是 mtime）。

### 5.2 定时（systemd user timer，**尚未安装**）

```bash
bash deploy/scripts/install-backup-timer.sh --dry-run            # 打印 unit 内容
bash deploy/scripts/install-backup-timer.sh --apply --enable     # 写 unit + daemon-reload + enable --now
```

unit：`person-fin-backup.service` + `.timer`（每日 03:00、`Persistent=true`、`TimeoutStartSec=3600`）。
需要「未登录也跑」时另执行 `loginctl enable-linger "$USER"`。
验收：`systemctl --user list-timers person-fin-backup.timer` 有记录；`systemctl --user start person-fin-backup.service` 后 `deploy/backups/` 多一份 tar。
> 2026-11-09 业主裁决：**暂不安装**，脚本已就绪，安装动作需单独授权。

### 5.3 恢复演练（季度）

```bash
bash scripts/restore_drill.sh                  # dry-run：只打印计划
bash scripts/restore_drill.sh --apply          # 真跑：解包 → 隔离容器 person_fin_drill(:6687) → 对拍 → 写报告 → 自动清理
```

检查项：`PRAGMA integrity_check`、`assets` 总数与 active（`superseded_by IS NULL`）数、`schema_version`、`audit_log` 行数与 hash 链 `GET /api/audit-log/chain/verify`。报告落 `.scratch/env-tiers/drills/<YYYYMMDD>.md`。
安全边界：项目名写死 `person_fin_drill`（断言不等于 `person_fin`/`person_fin_staging`）、独立网络、端口不得撞已知端口、只读副本、结束必清理。
> 2026-11-09 业主裁决：**首跑等指定时间**，脚本已就绪。

### 5.4 库回后退时审计链

回退旧库后必须复核 `GET /api/audit-log/chain/verify` 返回 `ok`；链断裂即视为恢复失败，按 §6 取证后再决定是否继续用该库。

---

## 6. 门禁与 hook

```bash
bash scripts/ci.sh                 # 默认档（pytest 全量 + tsc + next lint + doc-driven + public-safety），约 15 分钟
bash scripts/ci.sh --fast          # 快档（契约子集 + tsc + 两个 check），约 1 分钟
bash scripts/ci.sh --with-probe    # 追加浏览器探针（含 next build，约 3–5 分钟）
bash scripts/install-git-hooks.sh  # 装 .git/hooks/pre-push（调 ci.sh；可 --no-verify 绕过）
```

退出码全仓统一：`0` 通过 / `1` 断言失败 / `2` 环境缺失（缺 bun、`.venv`、`node_modules`、probe 时缺 curl）。

---

## 7. 常见故障

| 现象 | 原因 | 处置 |
|---|---|---|
| `staging.sh up` 起成了旧镜像 | `sudo` 清空环境，`STAGING_*_IMAGE` 失效回落 `:local` | 用 `sudo -n --preserve-env=…`（脚本已带）；`staging.sh` 会按容器 `Config.Image` 复核并在用 `:local` 时告警 |
| 库突然「变空」/ 行数为 0 | SQLite 找不到文件会**新建空库并跑迁移** | 一律「停容器/后端 → 改配置 → 搬文件 → 再起」；库缺失时 `staging.sh` 直接报错而不是造库 |
| Next 报 `Bad port` | 端口落在 WHATWG 屏蔽段（6665–6669） | 用 667x/668x/669x |
| `dev-down` 杀不到进程 | pidfile 丢失 | 脚本退化为按端口找 pid，并先判容器占用；仍不行就 `ss -ltnp` 手工定位 |
| `web` 打不开但 curl 正常 | 前端反代到后端正常，浏览器缓存/端口未切 | 核对 §1 端口表 |
| 备份 tar 很大（数百 MB） | `deploy/data` 含 evidence/backup/exports | 正常；`TimeoutStartSec=3600` 已放宽 |
| `release.sh` 在 ④ 之后失败 | 自动回滚已触发 | 看 `RELEASES.md` 该行 FAIL 与 `rollback.sh` 输出；容器应已回到上一版 healthy |

---

## 8. 未完成 / 待验（截至 2026-11-09）

- 真实发布一次（`release.sh --apply`）与「失败自动回滚」演练：脚本就绪，未执行（需授权窗口）。
- `prune-images.sh --apply` 实删：未执行（只验过保留策略与替身清单）。
- 生产端口切换（§4）：等本 runbook 就绪后走发布流程。
- 备份 timer 安装（§5.2）与恢复演练首跑（§5.3）：业主裁决延后。
- `env-tiers/spec.md` 目前只含 staging 段，升 L2 全量待业主冻结。
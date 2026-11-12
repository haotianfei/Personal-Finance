# person_f 部署包

本目录是 `person_f`（个人资产管理，FastAPI + Next.js + SQLite）的单机 Docker Compose 生产部署入口。

- 后端：`python:3.11-slim`，uvicorn 单 worker，公开 `6698`（2026-11-09 已切换；旧值 `6788`）
- 前端：`node:20-alpine`，Next.js 生产构建，公开 `6699`（2026-11-09 已切换；旧值 `6789`）
- 数据：`./data`（SQLite + evidence + backup + exports），挂载到容器 `/app/data`
- 镜像：`person_fin-backend:local` / `person_fin-frontend:local`
- Compose 项目名：`person_fin`

## 目录约定

```
deploy/
├── Dockerfile.backend      # 后端镜像定义
├── Dockerfile.frontend     # 前端镜像定义（多阶段）
├── docker-compose.yml      # 编排（端口/卷/健康检查）
├── .env.example            # 环境变量模板
├── RELEASES.md             # 发布/回滚台账（release.sh 与 rollback.sh 追加）
├── scripts/
│   ├── package.sh          # 源码 → build/ 构建上下文（排除数据/依赖/日志）
│   ├── init-data.sh        # 已有 data/ → 部署 data/（含 SQLite -wal/-shm）
│   ├── deploy.sh           # 构建 + 启动 + 等健康（手工裸跑用；日常升级走 release.sh）
│   ├── release.sh          # 门禁 → 回滚点 → 双 tag 构建 → 上线 → smoke → 台账（默认 dry-run）
│   ├── rollback.sh         # <tag> 回指 :local → up → smoke → 台账；无参列可用 tag
│   ├── prune-images.sh     # 镜像保留策略（5 个版本 + 3 个回滚点，默认 dry-run）
│   ├── install-backup-timer.sh  # systemd user timer（每日 03:00，默认 dry-run）
│   ├── smoke.sh            # 冒烟检查（支持 --backend/--frontend 基址）
│   └── backup-data.sh      # 数据目录打包备份 + 保留策略（默认 dry-run）
├── build/                  # 生成：构建上下文（不入库）
└── data/                   # 生成：运行时数据（不入库，需备份）
```

## 首次部署

```bash
cp .env.example .env
./scripts/package.sh                 # 生成 build/ 上下文
./scripts/init-data.sh ../data       # 复用开发库数据（含 WAL）
sudo chown -R 1000:100 data          # 容器以 uid 1000 运行
./scripts/deploy.sh
./scripts/smoke.sh
```

## 日常升级

```bash
git -C .. pull                                   # 或直接改代码
bash scripts/release.sh                          # 预检：打印 7 步（不改状态）
bash scripts/release.sh --apply                  # 门禁→回滚点→双 tag→上线→smoke→台账
```

失败会自动回滚到本次上线前打的 `rollback-*` 点。完整流程、回滚、端口切换窗口、备份恢复见
[`../docs/runbook-release.md`](../docs/runbook-release.md)。

## 运维

| 操作 | 命令 |
|---|---|
| 查看状态 | `sudo docker compose ps` |
| 跟踪日志 | `sudo docker compose logs -f backend` |
| 重启 | `sudo docker compose restart backend` |
| 备份数据 | `./scripts/backup-data.sh --apply`（不带 `--apply` 只看保留策略） |
| 停止 | `sudo docker compose down`（数据在 `./data`，不随容器删除） |

## 注意

- **端口选择**：现役 `6699`（前端）/ `6698`（后端）；2026-11-09 由 `6789`/`6788` 切换而来（见 runbook §4）。**避开浏览器受限端口**——Firefox 等对 `6665-6669`、`6000`、`10080` 等端口直接拒绝访问（报 "This address is restricted"），Next 自身也会拒绝 `6665-6669`，因此不再沿用旧的 `6666/6667`。端口规则：后端尾 8 / 前端尾 9，dev `667x` / staging `668x` / prod `669x`。

- **单 worker**：SQLite 单写者 + 进程内审计链/调度，后端固定 `--workers 1`，不要横向扩容。
- **数据一致性**：SQLite 使用 WAL，备份/迁移必须连 `person_fin.db-wal` / `-shm` 一起；`backup-data.sh` 已按目录整体打包。
- **PaddleOCR 模型**：首次文字识别会在线下载模型到 `paddle_cache` 卷；如需离线部署，提前把 `~/.paddlex` 放到本机（`package.sh` 会自动带入镜像）。
- **国内源**：后端 pip 用清华源、前端 npm 用 npmmirror；可用 `--build-arg PIP_INDEX_URL=` / `NPM_REGISTRY=` 覆盖。
- **同步目录风险**：若本目录位于文件同步目录内，运行中的 SQLite 不应被同步工具实时同步，建议把 `deploy/data` 排除在同步规则之外。
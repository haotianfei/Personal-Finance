# 数据库迁移与兼容性

> 主版本 = 数据世代。本文说明各版本之间的数据库结构关系与升级路径。
> English summary 见文末。

## 规则

1. **主版本 = 数据世代**：V1 记录式 / V2 快照式（v2.11 起）/ V3 智能识别（v3.0 起）/ V4 AI 辅助（v4.0 起）/ V5 身份与可信底座（v5.0 起）。
2. **世代内与 V2→V3→V4 自动迁移**：应用启动时自动执行、幂等（`backend/services/db_migration_service.py`）。v5.0.0 起迁移机制升级为**编号脚本 + `schema_version` 记账 + 迁移前自动备份 + fail-fast**（见下「V4 → V5」）。
3. **v1.x → v2+ 不兼容**：两种数据模型之间不存在自动升级路径。
4. **只支持升级，不支持降级**：旧代码打开新库可能报错。
5. **升级前先备份**：`./backup-and-restore.sh`（或直接复制 `data/*.db`）。

## 结构变化一览

| 版本 | 结构变化 | 迁移方式 |
|------|----------|----------|
| v1.x | 记录式：`asset_records` + 字典表（`fund_types` / `accounts` / `liquidity_ratings` / `asset_owners`） | —（起点） |
| v2.11.0 | 快照式：`assets`（快照）+ 属性体系（`attributes`） | ⚠️ 与 v1 不兼容 |
| v2.12.0 | 启动时 DROP V1 遗留五表（asset_records 等） | 自动（一次性清理） |
| v2.13.0 | `assets.side`（asset/liability），历史负数金额取绝对值并回填 side | 自动（幂等） |
| v3.0.0 | 截图录入：`screenshot_evidence` 表 + `assets.source_evidence_id` | 自动（幂等） |
| v3.1.0 | 来源分组：`source_groups` / `source_group_accounts`（种子 bantuou 组） | 自动（种子幂等） |
| v4.0.0 | `screenshot_evidence.supplement_rows` / `.engine`、`attributes.remind_enabled`、`llm_providers` 种子 | 自动（幂等） |
| v5.0.0 | 见下节「V4 → V5」：编号迁移 runner、身份层（`asset_items` 等）、`audit_log`、`idempotency_keys` | 自动（编号脚本 0001~0010，迁移前自动备份，fail-fast） |
| v5.1.0~v5.5.0 | 预览任务持久化（`preview_jobs`）、本金口径（`principal_changes` + `assets.cost_basis`）、内部转移（`transfer_events`）、血缘链接（`lineage_links`） | 自动（0011~0014） |
| v5.6.0~v5.9.0 | 三期闭环：目标与版本（`goals`/`goal_versions`）、归属收口（`asset_item_owner_changes`）、双阈值列（0017）、审计链（`audit_chain_state`/`audit_chain_seals`）、advisor 七表、阈值史（`allocation_target_revisions`）、复盘三表 | 自动（0015~0021） |
| v5.10.0 | 导入归属回填：记录级 owner → item 级归属史（纯数据、`NOT EXISTS` 幂等、无候选空跑） | 自动（0022） |
| v5（分类重构） | 记录级 EAV 下线：`attributes` / `asset_attributes` 删除；新增 5 张受控字典表（`asset_classes` 树、`institutions`、`channels`、`members`、`tags`）与 2 张关联/史表（`asset_item_tags`、`asset_item_attr_history` append-only）；`asset_items` 新增 6 列（`asset_class_id` / `liquidity_days` / `institution_id` / `channel_id` / `owner_id` / `risk_level`）与 `status='draft'` 草稿态；归属真相源改为 `asset_items.owner_id` 列（`asset_item_owner_changes` 降为纯审计流水） | ⚠️ 仅支持空库重建（原地升级会让 `allocation_targets` / `alert_rules` 的存量维度值变成读得出、写不进）；迁移号 0026~0028 |

## V4 → V5（v5.0.0，一期根基重构）

### 变更清单

| 域 | 变化 |
|------|------|
| v5 结构收口 | 0023 补 19 个读写路径索引 + `asset_item_names` 双 append-only 触发器；0024 按最小重建补 19 条 CHECK（`assets.amount >= 0` 等，逐表断言行数/索引/触发器复原）；0025 补 `asset_items.archived_effective_from`（归档生效日，ADR-0017 修订）。三者均只做加法、可重复执行（自动执行 0023~0025） |
| 迁移机制 | 新增 `schema_version` 记账表与 `backend/migrations/0001~0022` 编号脚本 runner：每脚本一事务、失败 fail-fast 中止启动、修复重启自动续跑；增量执行且有数据时**迁移前自动备份**（bundle manifest `reason=pre_migration`）。v4 老库首跑＝认领 0001~0007（不重复执行）＋实跑 0008 起。 |
| 身份层 | 新表 `asset_items`（资产项＝持久 ID 聚合根：display_name/channel/status/legacy_name）与 `asset_item_names`（显示名史，append-only）；`assets` 表重建：加 `asset_item_id`、`superseded_by`，删旧 `(asset_date, asset_name)` 唯一约束、改建部分唯一索引 `(asset_item_id, asset_date) WHERE superseded_by IS NULL`；`asset_name` 保留＝写入时显示名快照（改名不回写历史行）。`assets.id` 原值保留不重编号。 |
| 写语义 | 金额/方向/快照日为 write-once 账本事实：PUT 直改 → 409（改金额走 `POST /api/assets/{id}/correct`＝同日追加新行＋旧行 supersede）；改名走 `POST /api/asset-items/{id}/rename`（留史）；归档/取消归档走 asset-items 端点；批量创建/导入的 `overwrite` 静默覆盖移除，冲突返回显式清单。 |
| 审计 | 新表 `audit_log`（append-only，存储层触发器拒绝 UPDATE/DELETE）：谁/何时/何操作/前值后值/凭证引用/请求与幂等键；覆盖更正、截图采纳、改名、归档、建删、迁移执行等写路径；只读端点 `GET /api/audit-log`。 |
| 契约 | 全部非 2xx 响应统一错误信封 `{"error":{"code","message","details"}}`；写端点支持 `Idempotency-Key`（新表 `idempotency_keys`，同键同体重放首次响应、异体 422、7 天 TTL）；计算类读响应带口径元数据 `meta`（snapshot_date / basis / degradations）。备份 bundle manifest `generation` 位自此填 5。 |

**结构变化均为增量或受守卫重建；不删任何业务数据。**

### 备份与恢复（v5 起）

- **备份**＝单一 zip bundle（默认在 `data/backup/backup_YYYYMMDD_HHMMSS.zip`）：`manifest.json`（含 DB SHA256、凭证数、世代位）＋ SQLite 在线一致快照 DB ＋ `evidence/` 凭证目录；写盘原子改名；服务端默认保留最近 5 份（回滚包 `rollback_*` 永不删）。UI：「设置 → 备份与恢复」一键创建/下载。
- **恢复**：同一页面选 bundle 恢复——校验 manifest 与完整性 → 自动生成回滚包 → 替换 DB（并清理 WAL 伴生文件）→ 凭证**只增不删**合并回 `data/evidence/`。
- **旧裸 `.db` 兼容**：仍可恢复（仅数据库、不含凭证，响应附「不含凭证」警示）；旧备份原样列出、不迁移。

### 升级路径与回退

1. 直接部署 v5.0.0 并启动：建表 → 编号迁移自动执行（迁移前有数据即自动留 bundle）→ seed → 就绪。**无需手工操作。**
2. **fail-fast**：任一迁移脚本失败即中止启动（日志含失败编号与前置备份路径）；修复后重启自动续跑。
3. **不支持降级**：v5 代码写入的行含 `asset_item_id`，旧代码读不了新库；如需回退，代码 git revert 后必须**连同数据库用 pre_migration bundle 整库恢复**（残留 `schema_version`/`asset_items`/`audit_log` 等表对旧代码无害，但只 revert 代码不回滚库不可用）。

### FAQ

- **迁移失败怎么办？** 看启动日志中的失败编号与 `pre_migration` 备份路径；修环境/代码后直接重启（自动从失败编号续跑）；若数据已被牵连，用「备份与恢复」页（或恢复 CLI）载入该 pre_migration bundle 整库回滚后再升级。
- **手工备份在哪？** 「设置 → 备份与恢复」页即时创建并可下载；产物在 `data/backup/*.zip`。`rollback_*` 前缀包是恢复前自动生成的安全网。
- **升级前要自己备份吗？** 建议：升级前在备份页手工留一份 bundle（迁移自动备份只在增量执行时触发）。
- **改名后旧截图/旧查询还能命中吗？** 能：显示名史保留全部历史名，旧名解析回同一资产项；历史快照行的 `asset_name` 保持当时写法（时点语义）。

## v1 → v2+ 的手工迁移路径

1. 用旧版本（v1.4.1）导出 CSV；
2. 部署新版，在「导入」页按 CSV 模板重新导入（属性/账户在导入流程中重建）；
3. 或自行编写一次性脚本读取 `asset_records` 写入 `assets`。

> ⚠️ 不要用新版直接指向 v1 数据库：启动迁移会 DROP V1 遗留表，旧数据不会进入新模型。

## English Summary

- Major version = data generation. Migrations run automatically and idempotently at startup for v2.11+ (including v2 → v3 → v4).
- Since v5.0.0, migrations are versioned scripts (`schema_version` + `backend/migrations/0001~0022`) with fail-fast startup and an automatic pre-migration backup bundle. Upgrades only.
- v5 backups are single zip bundles (manifest + consistent DB snapshot + evidence files); legacy bare `.db` files can still be restored (DB only, without evidence).
- v1.x (record model) is **not compatible** with v2+: rebuild via CSV export/import; never point new code at a v1 database.
- Upgrades only — no downgrade support. Back up first (backup page or `./backup-and-restore.sh`).

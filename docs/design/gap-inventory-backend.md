# 事实层差距盘点：冻结设计 vs 现有后端

> 基准：`docs/design/system-architecture.md` v1.0（冻结版）+ `business-architecture.md` v1.1；采集日期 2026-09-27。
> 行文为事实层（有什么 / 没什么 / 证据），不含实施裁决——裁决见后续《实施策略裁决》。

## ① 7 模块 ↔ 现有代码归属

| 冻结模块 | 现状归属 | 判定 | 证据 |
|---|---|---|---|
| ledger | routers/assets·attributes·management + asset_service（`account_reminder_service` 自 ADR-0018 起仅派生覆盖度） | 部分 | 无资产项聚合根（身份=asset_name 字符串）`models.py:93`；无创建→活跃→归档状态机 |
| ingest | routers/screenshots + screenshot_service·ocr_engine·source_group_rules_service·preview_jobs | 已有 | `screenshot_service.py:1036,154,443` |
| evidence | ScreenshotEvidence + `data/evidence` + import_batches/export_history | 部分 | 凭证链 `models.py:109`、`:471` 查询端点；无审计日志表、无独立对账流 |
| valuation | routers/analysis·proportion·alerts·liquidity_ratings + 同名 services | 部分 | B1/B3/B4 已有；B2 本金/升值/TWR-XIRR 零实现（`models.py` 无 cost_basis/units/nav 位） |
| planning | routers/allocations + allocation_service | 部分 | allocation_targets 仅「当前态」无版本史 `models.py:75`；无目标金额/期限/GoalVersion；glide path/双阈值缺失 |
| advisor | 无 | 缺失 | 无建议/信息集/复盘/门任何代码 |
| platform | db_migration·backup·scheduler·ocr_engine·llm 客户端 | 部分 | 存在但形态与冻结版不符（见④） |
| D 家庭（无模块） | attributes type="owner" + routers/asset_owners + N:M asset_attributes | 部分 | `models.py:141,164`；无归属变更留史、无家庭池、analysis 无 by-owner 趋势（`analysis_service.py:189-356` 四维度不含 owner） |

## ② 聚合与不变量 ↔ models.py

- 快照 write-once ｜可改写可删：PUT /{id}、DELETE、batch-update、`_upsert_asset` 静默覆盖 amount ｜缺失｜`routers/assets.py:287,303,311`；`screenshot_service.py:1408-1416`
- `(资产项,快照日)` 唯一、冲突显式返回 ｜按 (asset_date,asset_name) 唯一且 upsert 静默覆盖 ｜部分｜`models.py:93`
- 本金口径变更史+快照行冗余 cost_basis ｜无表无列 ｜缺失｜`models.py:90-104`
- 转移事件 ｜无 ｜缺失｜—
- 血缘链接（改名/合并/拆分）｜无；改名即断身份（match 按名 fuzzy）｜缺失｜`screenshot_service.py:298`
- 共有归属变更留史 ｜N:M 现值可改、无史 ｜部分｜`models.py:156`
- 目标版本状态机（草案→生效→被取代）｜无 ｜缺失｜—
- 建议状态机+信息集 write-once ｜无 ｜缺失｜—
- 更正=追加+审计痕迹 ｜更正=直接 UPDATE ｜缺失｜`asset_service.py:216`

## ③ 数据流 F1~F4

- F1「存证先于识别」｜现状识别后才存证：上传落临时文件，apply 时才写 `data/evidence/` ｜缺失｜`routers/screenshots.py:71`；`screenshot_service.py:1453`
- F1 校正缝（parse 后 preview 前）｜与冻结一致 ｜已有｜`routers/screenshots.py:111,192`
- F1 AI 校验纯信号不落库 ｜已有 ｜已有｜`llm_verification_service.py:10`；`routers/screenshots.py:769`
- F1 人裁决→采纳单事务+伴生审计（supplements）｜已有 ｜已有｜`screenshots.py:336`；`screenshot_service.py:1430,576`
- F1 对账差异高亮供人处置+独立重查｜**已裁决不做（字段删除）**：该字段从未有比对逻辑，且差额无法区分「解析漏读」与「截图上本就有意剔除」，故不设报警（ADR-0021，迁移 0030 删列）｜不做｜`docs/adr/0021-删除凭证对账锚点字段.md`
- F2 升值→目标→双阈值再平衡引导 ｜偏离分析/建议/预警已有，升值与双阈值无 ｜部分｜`allocation_service.py:213,599`
- F3 建议闭环 ｜无 ｜缺失｜—
- F4 成员/家庭两级视图 ｜无 ｜缺失｜—
- 慢管线「DB 任务表+重启续跑」｜preview job 纯内存 dict，重启即失 ｜缺失｜`services/preview_jobs.py:36`

## ④ 存储与迁移

- 单库 WAL+凭证目录 ｜已有 ｜`database.py:21`；`data/evidence`
- 四区表族隔离+写端口按模块隔离 ｜单包混排，无模块边界 ｜缺失｜backend/routers·services 平铺
- 手写幂等迁移 ｜机制同型已有（inspect+ALTER ADD COLUMN） ｜已有｜`db_migration_service.py:28`
- `schema_version` 表+编号顺序执行+迁移前自动备份 ｜均无 ｜缺失｜`db_migration_service.py` 全文无 version 表/备份调用
- 金额恒正+side+字符串 Decimal ｜恒正+side 已有；序列化未统一约定 ｜部分｜`models.py:99-100`
- 备份=DB+凭证同备份、在线备份 API/checkpoint、世代校验、留 N ｜仅 shutil.copy(DB) 裸拷、不含 evidence、恢复只校验表名 ｜缺失/部分｜`backup_service.py:53,127,170-172`；`routers/backup.py:179`

## ⑤ 信任架构

- advisor 写端口结构不可达（导入边界+测试锁定）｜模块不存在故「空真」，无边界检查/锁定测试 ｜缺失｜—
- append-only 审计日志覆盖全部写路径（谁/何时/前值后值/凭证）｜全库无 audit 表；仅三孤点：凭证链、supplement_rows JSON 审计、http request_id 日志 ｜缺失｜`models.py:127`；`main.py:31`
- hash-chain 分期（三期前）｜无 ｜缺失（与拍板 2 时点相符，属预期未启动）
- 机器产出不过人永不写入 ｜F1 已有 ｜已有｜`screenshots.py:336`
- LLM 不做算术+确定性比对门 ｜比对纯函数已有；「引用可解析/信息集一致」门无 ｜部分｜`llm_verification_service.py:274`
- 模型/prompt 版本入审计 ｜仅 engine 审计列，无 prompt/模型版本 ｜部分｜`models.py:129`

## ⑥ API 契约风格

- 统一错误信封 `{error:{code,message,details}}` ｜FastAPI 默认 detail 字符串 ｜缺失｜`routers/assets.py:220`；`main.py:98`
- 一切写操作幂等键 ｜x-request-id 仅日志关联，非去重 ｜缺失｜`main.py:31`
- 快照冲突显式返回 ｜静默覆盖 ｜缺失｜`screenshot_service.py:1408`
- 读响应携口径元数据（快照日/口径版本/降级标注）｜summary 带 latest_date；无口径版本、无「率不可得/缺数据 carry-forward」标注 ｜部分｜`analysis_service.py:118`
- 契约冻结→前后端并行工作流 ｜已有（`docs/design` + `.scratch` spec 实践）｜已有

## 最值得注意的结构性差异

1. **身份模型是根差**：冻结版以持久资产项 ID 为聚合根、改名留史；现状以 asset_name 字符串为身份键，快照行直接改写/删除，write-once、更正追加、血缘、冲突显式化等约 6 项冻结要素都悬在这个缺失的身份层之上。
2. **不变量 8「全程审计」无承载物**：库中不存在任何审计日志表；可信性目前只由截图凭证链、补录 JSON、request_id 日志三个局部机制分担。
3. **迁移冻结版与实现形态分叉**：拍板 1 要求 schema_version+编号顺序执行+迁移前备份；现为 inspect 驱动的幂等 ALTER，无版本表、无前置备份——「幂等」重合、「可评审编号史+兜底」不重合。
4. **备份不满足追溯链完整性约束**：DB 裸拷贝（WAL 模式下非一致快照）且不含凭证目录，与 R-11/§4「两者必须同备份」直接冲突。
5. **二三期域整体为零而非半成品**：本金口径/升值、目标版本化、advisor 全链路、家庭两级视图、慢任务落库续跑均无代码——与演进路线（一期个人账本为当前域）的分期位置一致，但错误信封/幂等键/口径元数据等契约风格项属一期范围亦未启动。
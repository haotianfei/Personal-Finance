# append-only 审计日志：统一操作留痕，存储层强制 write-once

**状态**：已接受（2026-10-01，随 v5.0.0 落地）。

**背景**：不变量 8 要求「任何写入/更正/采纳/驳回可回答谁、何时、何操作、前值后值、依据什么」，但 v4~v5 一期前全库无审计承载物（差距盘点⑤），痕迹散落多处：凭证链（截图来源）、`assets.superseded_by` 与 `asset_item_names`（数据内痕迹）、`schema_version`（迁移记账）、`supplement_rows`（补录 JSON）。步骤 3 已在 ledger 写单点产出结构化 change dict（`{entity,row_id,action,before,after,evidence_ref}`）但仅回传响应不落库。规格：`.scratch/v5-audit/spec.md`；上游：`docs/design/system-architecture.md` §5、`.scratch/v5-identity/spec.md` §7。

**决策**：

- 新增 `audit_log` 表（编号迁移 0009）：`occurred_at / actor('owner'|'system') / entity / action / row_id / before_json / after_json / evidence_ref(可空 FK→凭证) / request_id / prev_hash`；actor 仅两值——单用户系统，机器产出无写入权（不变量 1），不存在第三种操作者。
- **append-only 双层强制**：存储层 BEFORE UPDATE/DELETE 触发器 `RAISE(ABORT)`（与快照 write-once 同哲学）；应用层 service 只有 `record()` 与查询函数、无任何修改端口（边界测试锁定）。
- **单点发射**：各写用例在业务事务 **commit 前** 调 `audit_service.record(db, change)`——业务回滚则审计不留（无孤儿审计行）；**审计写失败则阻断业务**（可信 > 便利，与步骤 2 备份失败中止迁移同姿态）。
- **与数据内痕迹互补**：`superseded_by`/显示名史回答「数据现在怎么解释自己」（随行永久、参与查询）；audit_log 回答「这次操作谁做的、依据什么」（跨实体统一视图）；before/after 为冗余快照，不作真源。
- 覆盖范围（一期）：更正 correct、截图采纳 apply（每 written 行一条、update 行覆盖前回读旧值、冲突行不留）、身份迁移 migration_run（runner 完成时 system 身份独立记账，不回补历史）、顺带 create/rename/archive/unarchive/update_attrs/delete；import/attribute CRUD 等列一期增量扩展（每项＝补 change dict＋record，零新机制）。
- `request_id` 自 contextvars 拾取（关联 HTTP 日志链）；v5 步骤 5 后与幂等键并列落痕；`prev_hash` 列预留恒 NULL——三期 hash-chain 以一次性编号脚本回填启用，本步零计算。

**Considered Options**：

- 复用 SQLAlchemy 事件监听器（before_flush 挂钩子）自动捕获全部写入 → 弃：捕获的是 ORM 变更行（列级 diff）而非业务语义（「更正」「采纳」），语义要靠调用点补；一期覆盖面小，显式单点发射更直白、可测。
- 仅靠日志文件（structlog）做审计 → 弃：日志可轮转可删、不可按实体/凭证反查、不满足「承载物」定位；数据库表与业务同事务才能保证「不留孤儿、不假成功」。
- 审计尽力而为（写失败不阻断业务）→ 弃：承载不变量 8 的表若可静默缺失，等于没有；同库单行 INSERT 失败概率极低，阻断的代价可接受（回滚＝git revert，残留表无害）。
- 一步到位 hash-chain（每行带前哈希）→ 弃（延期）：三期任务，本步只留列与顺序锚（id 自增＋occurred_at），避免过早绑定计算逻辑。

**Consequences**：

- 写路径新增一条硬约束：**任何新写入用例必须产 change dict 并 record()**，否则不算合法写入（code review 检查项）；批量采纳 N 行＝N 条审计同事务，千级行短事务可接受，性能异常再议分批。
- 审计行与数据内痕迹可能「不一致」时以后者为准（冗余快照语义），查询端点 `GET /api/audit-log` 只读（筛选 entity/row_id/action/evidence_ref/request_id/日期＋分页）。
- 真实环境若审计写入出问题时启动即报错（有意收紧）：回滚＝revert，表与触发器残留对旧代码无害。
- 不改 `superseded_by`/`asset_item_names` 的职责：它们仍是业务读取路径的一部分（净值取 active 行等），审计表纯旁路，不反哺业务查询。

---

## 修订（2026-11-07）

「任何新写入用例必须产 change dict 并 record()」这条硬约束的**判定口径、豁免与守卫**，改由 `backend/services/audit_contract.py` 的契约表承担（见 ADR-0027）：五类桶（值/口径/出处/离场/整库）定义「必须留痕」，豁免只有缓存派生态与派生字段回写两类且必须登记，`entity`/`action` 转为受控枚举，并由测试守卫「`app.routes` 写方法 × 契约表」双向相等。

**例外以契约表为唯一来源**——散落在 code review、口头约定或本 ADR 正文的豁免一律作废。单点发射、同事务、审计失败阻断业务三条不变量不变。

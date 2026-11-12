# ADR-0019：资产分类重构——放弃 EAV 单表，改为列化与字典表

**状态**：已接受（2026-11-02，业主拍板「重构，按最佳实践」）；随 `0026_asset_classification_refactor` 迁移落地。**修订（2026-11-04，已接受）**：D9 与 §3「草稿项不进任何汇总」由 **ADR-0020 D1** 取代——`draft` 项照常计入净值，并强制披露未补齐规模。
**上游**：ADR-0001（平面快照 + 属性分类法）、ADR-0002（资产/负债 side 字段）、ADR-0011（资产项级归属收口）、ADR-0017（归档项退出 as-of）、`docs/design/资产分类规范.md`（本 ADR 的规范正文）、`CONTEXT.md`「资产项」「属性」「归属」词条。
**下游**：`backend/models.py`、`backend/migrations/0026_asset_classification_refactor.py`、`backend/services/asset_service.py`、`backend/services/analysis_service.py`、`backend/services/alert_service.py`、`backend/services/account_reminder_service.py`、`backend/services/import_service.py`、`backend/services/screenshot_service.py`、`backend/routers/asset_items.py`、`backend/routers/assets.py`、`backend/routers/analysis.py`、前端资产项编辑/筛选/设置页（19 个文件）；`attributes` 与 `asset_attributes` 表删除。

## 1. 背景与问题

ADR-0001 用一张 EAV 表承载五种互不相干的维度：

```
attributes(id, name, type, parent_id, level, description, created_at, updated_at, remind_enabled)
    type ∈ {fund_type, liquidity_rating, account, owner, tag}    UNIQUE(name, type)
asset_attributes(asset_id → assets.id, attribute_id)             PK(asset_id, attribute_id)
```

一年后实测出四个无法靠清洗解决的问题：

**（1）基数无人保证，同维多值普遍存在。** 138 个资产项里有 **75 个 (项, 维度) 组合存在同维多值** —— 同一项的 `fund_type` 同时挂两个互斥节点、`liquidity_rating` 同时挂两档。schema 允许，应用层只在走特定 API 时拦，脚本、迁移、手工 SQL 都拦不住。

**（2）绑定在记录级，与「分类描述身份」的语义冲突。** `asset_attributes.asset_id` 指向 `assets`（一次测量），而「这个账户是货币基金」是资产项的固有属性。后果是三重的：分类打点极稀疏（15,892 行绑定摊在 4,282 条记录上，609 个 (项, 属性) 对里 229 个只覆盖个别记录）；查询要先把记录级绑定去重才能得到项级分类；记录级多值让「同维多值」在数据里合法化。

**（3）维度混装。** `account` 一维里同时装着账户、机构（银行/券商）、渠道（App/子钱包），以及根本不是账户的东西（`现金`、`物资`、`社保账户`）。「在哪家机构」只能靠字符串前缀匹配回答。

**（4）字典树有死枝。** `fund_type` 35 个节点里 14 个是没人会选的中间层；节点名里嵌全路径（`A-B-C` 直接当 `name`），`level` 与名字段数不一致；`owner` 只有 1 个节点且存的是真人姓名，还与「家庭池」保留名校验冲突。

**（5）`remind_enabled` 放错位置。** 它只对 `type='account'` 有意义，却是覆盖度与录入提醒分母的**唯一来源**（`account_reminder_service.py`、`analysis_service._coverage_for`）。一个本该属于「账户是否纳入核对」的语义，藏在分类字典的一列里。

判据很清楚：**问题在 schema，不在数据**。清洗只能把今天的矛盾抹平，明天仍会重新长出来。

## 2. 决策

**D1 放弃 EAV 单表，单值维度列化。** 四个必填单值维度写成 `asset_items` 的列，基数由 `NOT NULL` 结构性保证：

```sql
ALTER TABLE asset_items ADD COLUMN asset_class_id  INTEGER NOT NULL REFERENCES asset_classes(id);
ALTER TABLE asset_items ADD COLUMN liquidity_days  INTEGER NOT NULL CHECK (liquidity_days IN (0,1,7,30,90,180,365,9999));
ALTER TABLE asset_items ADD COLUMN institution_id  INTEGER NOT NULL REFERENCES institutions(id);
ALTER TABLE asset_items ADD COLUMN owner_id        INTEGER NOT NULL REFERENCES members(id);
ALTER TABLE asset_items ADD COLUMN channel_id      INTEGER          REFERENCES channels(id);
ALTER TABLE asset_items ADD COLUMN risk_level      TEXT             CHECK (risk_level IN ('R1','R2','R3','R4','R5'));
```

单值维度用列、多值维度用绑定表，是关系模型对该问题的直接解法；继续用 EAV 就永远要靠应用层守基数，而那正是第（1）条问题的成因。

**D2 分类从记录级上移到资产项级。** `asset_attributes` 删除；分类成为 `asset_items` 的属性。理由见第（2）条：分类描述身份，不描述测量。上移后 `(项, 维度)` 天然单值，绑定行数从 15,892 降到 138 量级（压缩 96%+），且与「改名不重建项」的身份模型一致。

**D3 三张独立字典表，不合并为 `ref_entities`。** `institutions`（含 `kind` 枚举）、`channels`、`members`（含 `role` 枚举）各自成表。曾考虑用 `ref_entities(id, kind, name)` 一张表加 `kind` 区分，被否决：合并后 `asset_items.institution_id` 的外键无法约束目标必须是机构而非成员，是伪多态，为一个纯命名上的便利牺牲真实完整性保证。

**D4 `liquidity_days INTEGER` + CHECK 白名单，不建档位 lookup 表。** 9 个稳定常量建表只换回一次 join。查询要回答「30 天内能动用多少」，`liquidity_days <= 30` 一个谓词即可；档位名在展示层按 days 分桶推导。原设想的「不可动用」档（days 为 NULL）被取消 —— 它会破坏 `NOT NULL` 与排序，退化为 `9999` + 标签。

**D5 `risk_level` 独立可空列，不进标签。** R1..R5 是有序标量，标签是无序集合。要做「加权平均风险等级」「高风险占比」就必须可聚合。

**D6 成员不存真人姓名。** `members.role ∈ {self, spouse, joint, child, nominee}`，`name` 存角色显示名。ADR-0011 的「家庭池」保留名及配套双向校验一并退役。

**D7 标签受控优先。** `tags.kind ∈ {controlled, free}`；`free` 标签必须由用户在设置页显式创建，禁止录入路径静默新建。否则标签会退化成又一个自由文本字段。

**D8 类别树叶子性与 side 一致性由触发器守，不用应用层。** 「类别必须是叶子」与「`side='liability'` ⟺ 类别 `side_scope='liability'`」都是跨行/跨表约束，CHECK 表达不了，应用层只能拦住部分写入路径。项目已有 append-only 触发器的先例（`asset_item_names`、`audit_log`），沿用同一机制。

**D9 增加 `draft` 草稿态，不放宽 `NOT NULL`。** 4 个必填维度会让「先建项、后补分类」的路径（截图采纳、CSV 导入）撞约束。解法是 `asset_items.status` 增 `draft`，配 `CHECK (status='draft' OR 四维非空)`；草稿项不进任何汇总，补齐即转 `active`。放宽 `NOT NULL` 会把 D1 的收益全部抹掉。

**D10 分类变更留痕，且不改写历史资产行。** 新增 `asset_item_attr_history`（`dim` 受 CHECK 约束，append-only，UPDATE/DELETE 由触发器拒绝）。分类变更可回溯，`assets.asset_name` / `assets.amount` 等历史行永不被回写 —— 与 ADR-0001「改名不动历史」同一条原则。

**D11 一次性落地，不分批上线。** `attributes` 是全部分析口径的输入：`analysis_service` 的维度聚合、`alert_service` 的 12 处 `dimension ==` 分支、`allocation_targets.dimension`（存字符串 `fund_type`/`liquidity_rating`/`account`，需重映射）、前端 19 个文件的筛选与展示。分批上线会出现「一半口径读新表、一半读旧表」的中间态，两套口径必然漂移（本仓库已有改名双计的教训）。因此与空库重建同一窗口：新库直接以 0026 建成，不做原地数据搬迁。

**D12 下线 `data_coverage` 及其催办语义。** `remind_enabled` 随 `attributes` 一起消失（第（5）条），其消费方按 ADR-0018 的既定方向处理：覆盖度退为纯派生描述性指标，录入节奏改由「估值日距今」承担。

## 3. 后果

**收益**

- 「同维多值」在 schema 层不再可能，无需任何校验代码。
- 「按机构聚合」变成 `GROUP BY institution_id`，不再是字符串前缀匹配。
- 分类查询从 15,892 行记录级绑定降到项级，且与记录数脱钩 —— 新增记录不再稀释分类覆盖率。
- 21 组合法条目（8 大类 29 叶子）、9 档流动性、3 张字典表，全部可被设置页完整枚举，不再有「中间层死枝」与「名字里嵌全路径」。

**代价与风险**

- **一次性的全口径切换**。后端 11 个文件、前端 19 个文件必须同窗口改完；中间态不可发布。这是 D11 的直接成本。
- **`allocation_targets.dimension` 的存量字符串需重映射**（`fund_type` → `asset_class_id`、`liquidity_rating` → `liquidity_days`、`account` → `institution_id`/`channel_id`）。映射表落 `attr_mapping.csv` 一并裁决，旧值无法映射的条目必须显式报错，不得静默丢弃。
- **`draft` 态引入新的状态机分支**：所有聚合谓词、导出、提醒都要加 `status <> 'draft'`；漏一处就会把草稿算进净值。验收必须逐条覆盖（汇总、趋势、占比、归因、导出、目标达成度）。
- **触发器是隐式约束**，出错信息不如 CHECK 直观，需要为三条触发器各写一条专门的失败用例。
- **仍有一部分判断无法自动化**：分类是否正确最终靠人。实测 138 项里 79 项旧分类与名称矛盾、20 项只能靠旧绑定、7 项待裁决 —— 这批分歧必须由业主逐条签字（`item_classification.csv` 的 `裁决_*` 列），迁移脚本不做任何自动推断式写入。

**未采取并记录在案的替代方案**

| 方案 | 否决理由 |
|---|---|
| 保留 EAV，应用层加基数校验 | 只拦住走该 API 的写入；脚本、迁移、手工 SQL 照旧能造多值。问题的根在 schema。 |
| `ref_entities` 合并三张字典表 | 伪多态，外键无法约束目标类别。 |
| `liquidity_ratings` 档位 lookup 表 | 9 个稳定常量，建表只增加一次 join。 |
| 放宽 4 个维度为可空 | 会把 D1 的收益全部抹掉，「未分类」重新变成合法状态。 |
| 拆资产/负债两棵类别树 | 同一产品可能两侧兼有（如积存账户透支），拆树后无法表达。 |
| 保留 `asset_attributes` 作为「记录级附加分类」 | 一个维度两处来源，必然出现两套口径。 |
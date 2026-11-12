# ADR-0017：归档资产项终止计量——as-of 聚合排除 `status='archived'`

**状态**：已接受（2026-10-30，业主拍板）；同日修订：排除**自归档生效日起**生效，不回溯抹除历史。
**上游**：ADR-0009（部分录入期沿用值与覆盖度）、ADR-0015（零额快照合法化）、`CONTEXT.md:20-22`（沿用值口径）、`backend/services/lineage_service.py:46-150`（血缘登记即归档前身项）、`backend/services/asset_service.py:177-192`（`set_item_status`，R-37）。
**下游**：`.scratch/item-identity-drift/spec.md` §2 D2 / §4 P0-2；`backend/services/analysis_service.py`（as-of 聚合）、`backend/services/proportion_service.py`（占比聚合）。

## 1. 背景与问题

ADR-0009 把「沿用值」定义为：某资产项在该快照日期（含）之前的最新一条有效记录，期内未更新则沿用、不置零。该口径本身没有变化——问题出在**计量何时终止**从未被定义：

1. 资产项可通过 `asset_items.status='archived'` 退出（`set_item_status`，R-37「归档＝人裁决动作」），但归档**只改状态、不写金额行**，最近一次值仍留在 `assets` 里。
2. 血缘登记（`lineage_service.register_lineage`）在合并/拆分/口径承接时**自动归档前身项**，即「归档」是合并链的常规产物，不是罕见操作。
3. as-of 聚合按 `row_identity()`（`asset_item_id`）分组，**只看 `assets.superseded_by IS NULL`，不看 `asset_items.status`**。于是归档项的最后一次值会被永久沿用计入净值——若同期已存在承接其持仓的新项，就是同一笔资产双算，且**永久**成立，不会自愈。
4. 2026-10-30 的一次趋势异常（单日假尖峰）暴露了同族问题的另一面：旧项未归零 + 新项首现 = 双算一期；当时旧项是 `active`，因此归档排除并不能单独解决该案例，但它是「身份迁移后旧项该不该继续贡献」这一族问题里唯一能被机器判定的信号。

## 2. 决策

**D1 归档＝自生效日起终止计量**：`asset_items.status='archived'` 的资产项，自 `archived_effective_from`（归档生效日）**当天起**不参与 as-of 聚合——净值、占比、分项趋势、预警、目标达成度在该日及之后按「不存在该身份」处理；**该日之前的历史测量值照旧计入**。其记录保留、可查、可审计。

> 首版曾写成「不参与任何 as-of 聚合」（无期排除）。实测该写法在血缘登记（合并/拆分，登记即归档前身项）时会连同整段历史一起抹掉：以合成形状推演，归档一个 10 万的项会让此前一年的每个点各少 10 万，制造出比本 ADR 要修的事故更大的假坑。故同为「终止计量」，但必须带生效日。

**D2 生效范围是聚合读端，不是数据**：不为归档项补写 0 值行，不改历史 `assets`。归档是身份级裁决，0 值是测量，两者不混用（对齐 ADR-0015 D3）。生效日存放于 `asset_items.archived_effective_from`（迁移 0025，可空）；为 NULL 的归档项视为「生效日未知」，对**所有**日期排除（保守，等价首版行为）。

**D3 口径单点定义**：归档过滤与 `row_identity()` 同处定义（`backend/services/snapshot_identity.py`：`archived_item_ids(as_of)` / `active_item_predicate(as_of)`），`analysis_service`、`proportion_service`、`alert_service`、漂移诊断共用同一谓词；谓词**必须带 as-of 日期**，禁止另写一份无日期版本。历史教训：改名双计 bug 正是同一口径被复制两份所致（`cd972c0`）。

**D4 与沿用口径的关系**：ADR-0009 不变。沿用仍适用于仍在计量的项；本 ADR 只回答「哪些身份、从哪一天起不再参与计量」。归档日之后，该项不再产生沿用值。

**D5 不做自动归档**：本 ADR 不引入任何「长期无更新即自动归档」规则。归档必须是人裁决（R-37）；`active` 项的长期沿用值仍按 ADR-0009 计入，界面以「含沿用值」标注。

## 3. 后果

- **当前库影响面为零**：`asset_items` 全部 `active`（无归档项），历史序列逐点不变。本 ADR 是为未来血缘登记与人工归档预置正确语义。
- **合并/拆分的正确姿势**：`lineage_service.register_lineage` 以其 `effective_date` 同时写 `principal_changes` 与该前身项的归档生效日，因此「历史归旧项、当日及之后归新项」由日期切分，不需要人工补 0 值行。
- 血缘链路首次变得有效：登记 merge/split 后前身项归档 → 自动退出净值，不再需要人工补 0 值行收尾。
- 读端口径需要一次接线（`analysis_service` + `proportion_service`），其余聚合点若各自复制 SQL 则可能漏改；这是 §2 D3 存在的理由，也是后续审计项。
- 若需撤回：删除谓词调用即可；迁移 0025 只加了可空列，回退无需数据回填。
- 手工归档（`set_item_status`）以「当天」为生效日；若需要回溯某个更早日期，必须显式传 `effective_from`（服务层参数已支持），否则会在归档点之后才终止计量。
- 已知限制：归档项的历史金额仍参与「历史快照求和」类查询（按日期直接 `SUM(amount)` 而非 as-of 的临时查询）。此类查询若用于对外口径，需显式声明是否排除归档项。
- 已知限制：逐项陈列类读模型（资产当前值 `/api/assets/current`）仍展示归档项的最后值，属陈列而非汇总，不构成口径违反。
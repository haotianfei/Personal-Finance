# 事实层差距盘点：冻结设计 vs 现有前端

> 基准：`docs/design/system-architecture.md` v1.0（冻结版）+ `business-architecture.md` v1.1；采集日期 2026-09-27。
> 行文为事实层（有什么 / 没什么 / 证据），不含实施裁决——裁决见后续《实施策略裁决》。

## ① 12 子域 ↔ 路由/组件覆盖

| 子域 | 现有前端 | 判定 | 证据 |
|------|---------|------|------|
| A1 快照账本 | `/records` 表格 CRUD + 属性挂接 + side 字段 + `/import` 批量 | **已有**（功能覆盖） | `app/records/page.tsx`, `lib/api.ts:60-178` |
| A2 采集与录入 | `/screenshot` 巨型页：预览→解析→待录入清单→AI校验→补录→apply；`/settings/source-groups` 校正规则管理 | **已有**（管线完整） | `app/screenshot/page.tsx`, `lib/api.ts:486-595` |
| A3 凭证·对账·审计 | 凭证存证展示 `getScreenshotEvidence`；**无对账 UI、无审计日志查看器、无血缘视图** | **部分**（仅凭证查看） | `lib/api.ts:530-531` |
| B1 净值现状 | 仪表盘趋势图 + `/analysis` 多期对比 | **已有** | `app/page.tsx`, `app/analysis/page.tsx`, `api.ts:216-230` |
| B2 本金与升值 | **无**：无 cost_basis/principal 字段消费、无升值率/TWR/XIRR 展示、无本金口径变更 UI | **缺失** | `types/index.ts` 无相关字段；`api.ts` 无相关端点 |
| B3 占比与流动性 | `/allocation` 占比图 + `/analysis` by-type/by-liquidity + DistributionList | **已有** | `app/allocation/`, `api.ts:319-334` |
| B4 预警信号 | `/alerts` 规则 CRUD + 结果展示（录入提醒横幅已于 ADR-0018 退役；新鲜度改由记录页「当前值」的 fresh/stale/missing 承担） | **已有** | `app/alerts/page.tsx`, `api.ts:364-378`, `components/records/CurrentValueTable.tsx` |
| C1 未来收益目标 | **无**：无目标版本管理、无金额+期限设定、无派生年化、无 glide path | **缺失** | `api.ts` 无 goal 端点；`types` 无 GoalVersion |
| C2 配置引导与再平衡 | `/allocation` 目标配置 + 偏离 + 调整建议（**无**多框架并存、无双阈值取先触发、无引导方向） | **部分** | `api.ts:337-361`, `components/allocation/TargetConfigModal.tsx` |
| C3 AI 调优建议 | **无**：无建议状态机 UI、无校验门展示、无引用列表 | **缺失** | 全仓库无 advisor 相关路由/组件 |
| C4 复盘与反哺 | **无**：无归因分类、无反哺提案 | **缺失** | 无相关代码 |
| D1 成员归属 | `/asset-owners` 管理 + `owner_id` 字段已贯穿各页 | **已有**（基础标注） | `app/asset-owners/page.tsx`, `api.ts:279-284` |
| D2 家庭视图 | **无**：无成员/家庭池两级视图、无归集报表 | **缺失** | 无相关路由/组件 |

## ② 关键流程 UI 支持

| 流程 | 现状 | 判定 | 证据 |
|------|------|------|------|
| **F1 待录入清单裁决** | `/screenshot` 已实现完整链路：识别清单→校正→AI 校验→逐行编辑/跳过→补录采纳→写入→凭证挂链 | **已有**（对齐冻结稿） | `screenshot/page.tsx` 全文, `api.ts:486-531` |
| **F2 偏离→再平衡引导** | `/allocation` 有偏离百分比展示+调整建议文案，**无**双阈值监控 UI、无回调方向引导、无框架选择/切换 | **部分** | `components/allocation/DeviationChart.tsx`, `TargetConfigModal.tsx` |
| **F3 建议→门→人审→复盘** | **完全缺失**（无 advisor 路由/组件/端点） | **缺失** | — |
| **F4 家庭归集** | 仅有 owner_id 字段管理；无两级视图、无家庭池虚拟成员、无共用归属裁决 | **缺失** | — |

## ③ 契约消费对齐度（§6）

| 契约要点 | 现有 api.ts | 判定 | 证据 |
|----------|------------|------|------|
| 错误信封 `{error:{code,message,details}}` | 仅读 `res.json().detail`（FastAPI 默认），非冻结格式 | **缺失** | `api.ts:24-30` |
| 幂等键（客户端 request-id） | 无任何幂等键机制 | **缺失** | `api.ts` 全文无 header/field |
| 口径元数据（快照日/口径版本/降级标注） | 响应无 metadata 字段，金额直接渲染 | **缺失** | `types/index.ts` AssetRecord 无口径位 |
| 金额恒正字符串 Decimal + side | **对齐**（side: 'asset'|'liability'，金额字符串） | **已有** | `types/index.ts`, `api.ts:189-192` |
| 快照 (资产项, 快照日) 唯一→冲突显式 | batchCreate 有 conflict_strategy（skip/overwrite/raise） | **已有** | `api.ts:74-81` |

## ④ 演进四期 × 既有 UI 可复用度

| 期 | 冻结范围 | 可复用现有资产 | 事实 |
|---|---------|--------------|------|
| 一期 | ledger+evidence+ingest+valuation(B1) | `/records`, `/screenshot`, `/import`, `/analysis`(总曲线), 仪表盘 StatCard | 高度复用；缺：凭证对账 UI、审计日志 |
| 二期 | 本金口径+转移事件+血缘+B2 | 仅 `/analysis` by-item 趋势可作升值起点 | 几乎全新 UI；可复用：AmountInput、Decimal 约定、side 字段语义 |
| 三期 | planning(C1/C2)+advisor(C3/C4)+B4 | `/allocation` 偏离展示可升级为 C2 引导；`/alerts` 规则结构可扩双阈值 | 中等复用；advisor 全模块新建 |
| 四期 | D1+D2 家庭视图 | `/asset-owners` 管理已有；owner 维度已贯穿记录 | 管理页复用；两级视图/报表全新 |

## 最值得注意的结构性差异

1. **B2 本金与升值为零落地**：设计将其列为"核心/差异化"，`api.ts` 中无 cost_basis、无 TWR/XIRR、无口径变更端点，`types` 无相关字段——二期垂直切片需从零搭建。
2. **C3/C4 建议闭环全缺**：设计定义了 advisor 模块完整状态机（生成→门→待审→采纳→复盘→反哺），前端无路由/组件/类型/端点，与现有 LLM 供应商配置的"校验"角色完全不同——现有 LLM 只做识别辅助，不产出决策建议。
3. **契约风格分歧**：冻结稿要求统一错误信封 `{error:{code,message,details}}` + 客户端幂等键 + 口径元数据，现有 `api.ts` 仍用 FastAPI 裸 detail + 无幂等 + 无降级标注——这是前后端并行开发的前提缺口。
4. **F1 实现超出设计描述**：截图管线已实现 peer-entry 双引擎对等（engine=ocr|ai）、AI 补录通道、校正规则反哺——设计冻结稿的 ingest 部分反而更保守，前端已走在前面。
5. **D2 家庭视图设计定"数据维度而非账号体系"，前端仅有 owner_id 标注无聚合归集**：无"家庭池虚拟成员"、无两级净资产切换，D1→D2 的跳跃需要全新的视图组件层级。
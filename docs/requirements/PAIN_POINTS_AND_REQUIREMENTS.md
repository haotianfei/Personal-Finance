# 痛点与需求记录

> 记录日期：2026-08-18
> 来源：用户访谈
> 状态：待分析/待实现
> 说明：本文件系 2026-08-22 依据会话记忆重建（原文件随旧快照目录被清理），内容为原始需求记录；文末附当前进度对照。

---

## 核心痛点

### P1: 录入麻烦 —— 多源数据手工聚合
**现状**：需打开多个 App（银行、券商、支付宝、微信、公积金等）查看余额，再手工逐条录入
**影响**：
- 频次低（月度/周度），数据滞后
- 易漏录、错录
- 无法做到日级/实时级追踪
**期望能力**：
- 多账户 CSV/Excel 批量导入
- 字段映射配置（不同导出格式自动识别）
- 增量同步（仅导入新增/变更记录）
- 最近值预填 / 模板化录入
- 可扩展：未来对接开放银行/券商 API

### P2: 无法区分工资收入与投资收益
**现状**：只有资产快照，无流水概念，所有资金变动混在一起
**影响**：
- 不知道「本金投入」vs「收益增长」
- 无法计算真实收益率（XIRR/MWRR/TWRR）
- 无法做现金流分析、预算规划
**期望能力**：
- **Transaction（流水）领域模型**：收入/支出/转账/投资收益/分红
- 自动分类规则引擎：
  - 工资：固定日期、固定金额、对方账户固定、备注含"工资/薪资"
  - 投资收益：来自投资类账户、关联基金/股票分红、金额波动
  - 转账：账户间资金流转，净值不变
- 现金流报表：经营性/投资性/筹资性现金流
- 本金 vs 收益 分离视图

### P3: 无法根据总投资资产设定合理年化，指导投资分配
**现状**：只有历史回顾分析，无前瞻规划能力
**影响**：
- 资产配置靠感觉，无量化依据
- 不知道当前配置偏离目标多少
- 无再平衡触发机制
**期望能力**：
- **目标资产配置模型**：按风险等级/资产类别设定目标权重
- **年化预期建模**：基于历史数据/市场假设的预期收益率矩阵
- **再平衡建议**：偏离阈值触发、最小交易成本调整方案
- **风险预算**：最大回撤、VaR、波动率约束
- 可视化：当前 vs 目标配置对比、预期收益区间、再平衡操作单

### P4: 历史手工录入出错，无查证凭证
**现状**：
- 手工录入无审计轨迹
- 发现错误时无法追溯来源（是哪个 App 导出的？哪一行？）
- 无法对账：系统余额 vs 实际账户余额
- 修改记录后无痕迹
**影响**：
- 数据可信度低，不敢基于此决策
- 审计/对账极其困难
- 错误传播：早期错误影响后续所有分析
**期望能力**：
- **原始凭证存证**：每笔导入/录入关联源文件/原始行数据
- **完整审计日志**：谁、何时、什么操作、修改前值、修改后值、来源凭证
- **对账功能**：系统快照 vs 外部导出数据一键比对，差异高亮
- **版本控制/时间旅行**：任意时间点的资产快照可还原
- **数据血缘**：Asset ← Transaction ← RawImport ← SourceFile

---

## 领域模型演进需求

### 现有模型（Asset Tracking）
| 实体 | 现状 | 需扩展 |
|------|------|--------|
| Asset | 快照记录 | 增加 `source_trace_id` 关联凭证 |
| FundType | 字符串 | 标准化枚举 + 层级分类 |
| Account | 字符串 | 增加 `institution_type`、API 凭证引用 |

### 新增核心模型

#### 1. Data Ingestion（数据采集域）
```
DataSource (数据源)
  - id, name, type(BANK/BROKER/ALIPAY/WECHAT/MANUAL)
  - config: {export_format, field_mapping, schedule}
  - credentials_ref (加密存储)

IngestionJob (采集任务)
  - id, data_source_id, status, started_at, completed_at
  - raw_file_ref (原始文件存储引用)
  - stats: {total_rows, success, failed, duplicates}

RawTransaction (原始流水)
  - id, ingestion_job_id, raw_row_number
  - raw_data (JSON, 保留原始所有字段)
  - parsed_data (标准化后字段)
  - parse_status: SUCCESS/FAILED/PENDING_REVIEW
  - error_message

NormalizationRule (标准化规则)
  - id, data_source_id, field_mapping (JSON)
  - amount_parser_config, date_format, category_rules (分类规则)
```

#### 2. Cash Flow Management（现金流域）
```
Transaction (流水)
  - id, account_id, asset_name_id, transaction_date, posted_date
  - type: INCOME/SALARY/INVESTMENT_RETURN/TRANSFER_IN/EXPENSE/TRANSFER_OUT/FEE/TAX
  - amount (正数，方向由 type 决定), balance_after (可选)
  - counterparty, description, raw_description, category
  - confidence_score, source_trace_id → RawTransaction
  - created_at, updated_at, version (乐观锁)

CashFlowStatement (现金流报表)
  - period, operating_cash_flow, investing_cash_flow, financing_cash_flow, net_cash_flow

IncomeBreakdown (收入拆解)
  - period, salary_total, investment_return_total, other_income_total, details
```

#### 3. Investment Planning（投资规划域）
```
TargetAllocation (目标配置)
  - allocations: List<AllocationTarget> (asset_class, fund_type, target_weight, min/max_weight)

ExpectedReturnModel (预期收益模型)
  - assumptions: List<ReturnAssumption> (asset_class, expected_return, volatility, correlation_matrix)
  - methodology: HISTORICAL/BLACK_LITTERMAN/RISK_PARITY/CUSTOM

RebalancingPlan (再平衡计划)
  - actions: List<RebalanceAction> (asset_name, current_weight, target_weight, trade_amount, trade_type)
  - estimated_cost, expected_tracking_error, status

RiskBudget (风险预算)
  - max_drawdown, var_95, max_volatility, asset_class_limits
```

#### 4. Audit & Evidence（审计与凭证域）
```
AuditLog (审计日志)
  - entity_type, entity_id, action: CREATE/UPDATE/DELETE/IMPORT/RECONCILE
  - actor, timestamp, before_state, after_state, source_evidence_id

SourceEvidence (源凭证)
  - source_type: CSV_UPLOAD/API_SYNC/MANUAL_ENTRY/RECONCILIATION
  - file_ref, file_hash (SHA256), row_range, parsing_config_snapshot

ReconciliationRecord (对账记录)
  - account_id, as_of_date, system_balance, external_balance, difference
  - status: MATCHED/MISMATCH/INVESTIGATING/RESOLVED
  - details: List<ReconciliationItem>

DataLineage (数据血缘)
  - asset_id → transaction_ids → raw_transaction_ids → ingestion_job_id → source_file
```

---

## 实施优先级建议

| 优先级 | 阶段 | 核心交付 | 解决痛点 | 预估工期 |
|--------|------|----------|----------|----------|
| P0 | Phase 0 | 手工录入体验优化（最近值预填、批量模板、快捷键、字段记忆） | P1(缓解) | 1-2 周 |
| P1 | Phase 1 | Transaction 领域模型 + CSV 导入增强（自动识别收入/支出/转账、原始行存证） | P1、P2、P4(部分) | 3-4 周 |
| P1 | Phase 2 | 审计日志 + 源凭证存证 + 对账功能 | P4(核心) | 2-3 周 |
| P2 | Phase 3 | Cash Flow Use Cases - 现金流报表、工资/收益分离视图、分类规则引擎 | P2 | 3-4 周 |
| P2 | Phase 4 | Data Ingestion - 多账户配置、增量去重、解析规则可视化编辑 | P1(核心) | 3-4 周 |
| P3 | Phase 5 | Investment Planning - 目标配置、年化预期、再平衡建议、风险预算 | P3 | 4-6 周 |

---

## 技术债与约束

1. 现有数据迁移：现有 Asset 记录需补全 `source_trace_id`，可生成虚拟凭证标记为 `MANUAL_ENTRY_LEGACY`
2. 存储成本：原始流水/凭证文件随时间增长，需归档策略（冷热分离）
3. 隐私合规：原始导出文件含敏感信息，需加密存储、访问控制、脱敏展示
4. 性能：对账/血缘查询涉及大表关联，需物化视图/预计算
5. 向后兼容：API 版本管理，前端渐进式升级

---

## 验收标准（Definition of Done）

### P4 专项验收
- [ ] 任意 Asset 记录可追溯到原始 CSV 行（文件名、行号、原始字段值）
- [ ] 任意修改操作有完整审计日志（前值、后值、操作人、时间、来源凭证）
- [ ] 一键对账：上传最新导出 CSV，自动比对系统快照，差异逐行高亮
- [ ] 数据血缘图谱可视化：Asset → Transaction → RawTransaction → SourceFile
- [ ] 误删/误改可一键回滚到任意历史版本

---

## 相关文档

- [领域模型设计](../architecture/DOMAIN_MODEL.md) - 待创建
- [上下文地图](../architecture/CONTEXT_MAP.md) - 待创建
- [ADR 记录](../adr/) - 待创建

---

## 当前进度对照（2026-08-22 更新）

| 需求 | 状态 | 说明 |
|------|------|------|
| **P1 多源录入** | ⚠️ 部分 | CSV 导入已有（含预览/冲突/新属性确认）；字段映射、增量同步未做。「帮你投截图→自动录入」为 P1 延伸，待启动 |
| **P2 区分工资/收益** | ❌ 未做 | 无 Transaction/流水；需引入流水模型（将为 side 之后的下一步大功能） |
| **P3 投资配置/年化** | ✅ 基本完成 | allocation_targets 目标配置、偏离告警、调整建议、自定义阈值均已实现 |
| **P4 审计/对账** | ❌ 未做 | 仅有 import_batches/export_history（轻量批次），无审计日志、无对账、无血缘 |

**相关文档现状（2026-08）**：已建 `CONTEXT.md`（领域术语）+ `docs/adr/0001`（扁平快照+属性分类法）+ `docs/adr/0002`（负债=正数+side 字段）。
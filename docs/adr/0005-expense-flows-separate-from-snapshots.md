# 支出流水独立于资产快照：固定支出模板 + 一键生成，支出不参与净值计算

**修订（2026-11-04，已接受）**：文中支出与模板对「账户属性（`attributes.type="account"`）」的引用已由 **ADR-0019** 改指 `institutions`（`fixed_expense_templates.account_id`/`expenses.account_id` → `institutions.id`）。支出流水独立于资产快照、不参与净值的结论不变。

新增「支出录入」：固定支出（周期性）与临时大额支出（一次性）作为一类新的**流水**数据落库，新增 `expense_categories`、`fixed_expense_templates`、`expenses` 三表；固定支出经「模板 + 一键生成当期记录」产生流水，生成必须用户手动触发并预览确认；支出与 `assets` 快照分表存储、互不改写。契约冻结于 `.scratch/expense-entry/spec.md`。

**决策**：

- 支出是「流」，资产是「快照」，**分表存储**；支出**不参与净值计算**（`assets` 余额快照本身已反映花掉的钱），支出账本服务于负担/结构/波动分析。本期不引入收入，因此不做结余率。
- 固定支出采「模板 + 手动一键生成」而非后台自动生成：`(template_id, period)` 唯一约束保证幂等；生成记录可改实际金额/日期（实际值覆盖预期值），不触发调度。
- 支出分类**独立成 `expense_categories` 维度表**，不复用资产属性分类法（`attributes`）。
- 金额一律正数存储、字符串序列化（沿用 assets 约定）；**固定支出月负担**按「月付 + 季付/3 + 年付/12」归一化（求和后统一保留两位）。

**Considered Options**：

- 复用 `assets` 表 + 扩展 side（如 `side="expense"`）→ 弃：快照按 `(asset_date, asset_name)` 唯一、每期一条，语义是余额而非发生额；混用会破坏快照不变式与净值计算。
- 通用 Transaction 流水模型（收入/支出/转账/投资收益，见 `docs/requirements/PAIN_POINTS_AND_REQUIREMENTS.md` P2 设想）→ 本期不做：范围收敛到支出侧；三表结构预留批量导入（其它小额支出导入）与后续扩展空间。
- 后台自动生成预期记录（预期/实际两态）→ 弃：需调度、状态回填、缺漏处理，复杂度高；「手动触发 + 预览确认」已消除漏记的主要痛点。
- 支出分类复用 `attributes`（新 `type`）→ 弃：attributes 绑定资产属性分类法（fund_type/account/…），消费语义独立，独立表更干净、删除守卫更直接。
- 大额支出支持分期 → 本期不做：先按单条记录 + 账户关联。

**Consequences**：

- 后端三 router（`/api/expense-categories`、`/api/expense-templates`、`/api/expenses`）+ `expense_service`；前端 `/expenses`（支出流水 + 固定支出项目双 tab、生成向导弹窗）、`/expense-categories` 管理页、首页「本月支出概览」卡片。
- 支出与资产分析暂为并置（仪表盘卡片）；分析页「期间净资产变化 vs 支出」对照留下一迭代。若未来引入收入，可在本流水模型上补结余率/现金流报表。
- 删除守卫：被引用的支出分类不可删（400）；删除模板解绑已生成记录（`template_id` 置空）但不删流水。
- 季/年付折算用算术除法、求和后统一舍入，不改模板金额，误差不影响单条支出记录的准确值。
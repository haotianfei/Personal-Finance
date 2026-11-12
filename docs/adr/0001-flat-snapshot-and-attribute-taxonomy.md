# 扁平快照 + 属性分类法（删除 V1 归一化表）

**修订（2026-11-04，已接受）**：本文确立的「统一 EAV 属性分类法（`attributes` + `asset_attributes`）」已由 **ADR-0019** 取代——改为 `asset_classes`/`institutions`/`channels`/`members`/`tags` 五张专用字典表 + `asset_items` 列化，`attributes`/`asset_attributes` 已由迁移 `0027` 删除。本文其余结论（扁平快照、不用 V1 归一化表）仍然有效。

系统曾并存两套资产模型：V1 的归一化表（`asset_records` 带 `fund_type_id`/`account_id` 等外键，配 `fund_types`/`accounts`/`liquidity_ratings`/`asset_owners` 专用参考表）与 V2 的扁平快照 `assets` + 统一属性树 `attributes`。两者数据失同步，且围绕 V1 的导入导出代码部分已损坏。2026-08 清理时决定彻底删除 V1（5 张表 + 约 4000 行代码），统一保留 V2：`assets` 每行是一条资产项在某日期的金额快照，`attributes` 用 `type` 字段区分「资产类型 / 账户 / 流动性评级 / 所有者」四种维度并构成层级树，`asset_attributes` 多对多挂接。

**Considered Options**：曾考虑把「资产项」与「估值」拆成 Holding/Valuation 两个实体，以及用归一化外键表表达维度 —— 均因单人、读多写少、数据量小（千级行）而判定为过度设计，弃用。
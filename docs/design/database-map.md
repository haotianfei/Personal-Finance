# 数据库结构图（v7.1.0）

> 依据：`backend/models.py`、`backend/migrations/0001~0033`、`backend/services/db_migration_service.py`（逐字核对，2026-11-06）。
> 定位：**现状结构快照**（描述性文档）；设计与演进依据见 `system-architecture.md` §4 与 `docs/MIGRATION.md`。
> 上一版 v7.0.0 覆盖到迁移 0032（v5 分类重构 ADR-0019/0020、解析规则落库、凭证命名/图像元数据 ADR-0022/0023）。本版增补凭证按来源组去重与复合唯一索引（evidence-dedup-index，ADR-0024）：**47 张表、39 个触发器、67 个显式索引、迁移 0001~0033**，`attributes`/`asset_attributes` 两张 EAV 表已物理删除。

## 一、总览

- **形态**：单 SQLite 文件（默认 `data/person_fin.db`，`DATABASE_PATH` 可覆盖）。引擎强制 `journal_mode=WAL` + `foreign_keys=ON`；迁移 runner 用 stdlib sqlite3 独立连接（表重建所需，0008/0026/0027 显式断言 FK off）。
- **物理表总数：47 张** ＝ ORM 映射 44 张（create_all 建表）＋非 ORM 3 张（`idempotency_keys`、`preview_jobs`、`schema_version`，由迁移/runner 裸 SQL 建立与访问）。另凭证原图为文件系统（`data/evidence/`，非表）。
- **触发器 39 个 / 19 张表**；显式索引 67 个。
- **启动链**：create_all → `run_migrations()`（每脚本一事务、失败 fail-fast、增量且有数据时前置备份 bundle）→ seed（source_groups、llm_providers，幂等不编号）→ `audit_chain.ensure_chain()` 自愈校验。v4 老库首跑认领 0001~0007（`CLAIM=True`，不跑本体）。
- **schema_version 现状**：**33 行**（1~7 `baseline=1` 认领，8~33 实跑）。0026/0027/0028 于 2026-11-03~04 的空库重建链路上一并登记；0029（凭证来源组外键）、0030（删 `anchor_amount`）、0031（教学规则拆表）已于 2026-11-05 停服窗口在运行库执行；0032（凭证两列 + 清脏原始名）同日随后端容器重建随启动链执行；0033（凭证按来源组去重 + 复合唯一索引）随本批代码上线、由启动 runner 执行。0032 之后的运行库实测：`screenshot_evidence` 11 列、**28 行 / 23 个不同 sha**（spec 冻结时口径；2026-11-06 副本演练时已是 **34 行 / 31 个不同 sha**，期间 ids 16、18 连同其文件已被移除、又新增 9 条上传）、`shot_upload_*` 脏名 0 条、`image_meta`/`recognition_json` 尚未写入、`foreign_key_check` 空、`quick_check=ok`。0033 执行后：spec 口径 28 → 23 行；2026-11-06 副本演练实测 **34 → 31 行**（3 组同组重复的零引用行删除，另 2 组的零引用行已在此前被清掉，见 §四迁移表）。
- **七区归位**（系统架构 §4）：

| 区 | 表 |
|---|---|
| 账本区（append-only/write-once） | `assets`、`asset_items`、`asset_item_names`、`asset_item_attr_history`、`principal_changes`、`transfer_events`、`lineage_links`、`asset_item_owner_changes`、`audit_log`、`audit_chain_state`、`audit_chain_seals` |
| 凭证区 | `screenshot_evidence` + 磁盘原图 |
| 参考区（可变） | `asset_classes`、`institutions`、`channels`、`members`、`tags`、`asset_item_tags`、`source_groups`、`source_group_accounts`、`allocation_targets`、`allocation_target_revisions`、`alert_rules`、`expense_categories`、`fixed_expense_templates`、`llm_providers` |
| 规划区 | `goals`、`goal_versions` |
| 建议隔离区 | `advisor_info_sets`、`advisor_info_entries`、`advisor_suggestions`、`advisor_suggestion_items`、`advisor_decisions`、`advisor_jobs`、`advisor_settings`、`advisor_reviews`、`advisor_review_suggestions`、`advisor_review_proposals` |
| 会话/任务区与流水 | `expenses`、`import_batches`、`export_history`、`auto_export_rules`、`idempotency_keys`、`preview_jobs` |
| 系统记账 | `schema_version` |

- **类型约定**：金额 `Numeric(15,2)`；比率/阈值 `Numeric(5,2)`（例外均有 ADR 明载：`goal_versions.target_rate_annual(8,4)`、`lineage_links.split_ratio(6,4)`）；**金额非负**（0＝该日余额为零，ADR-0015）+ `side`（例外：`principal_changes.new_amount` 带符号）；append-only 新表用 ISO TEXT 时间制，旧表 `DATETIME`；分类维度的值域一律由 `CHECK` 表达（见各表与 §五）。

## 二、逐表清单

### 账本区

**1. `assets`** — 每日快照行（write-once 金额本体；读取出 `superseded_by IS NULL` 的 active 行）
- 列：`id` PK｜`asset_name` VARCHAR(100)（**写入时显示名快照**，改名不回写）｜`amount` NUMERIC(15,2) NOT NULL（非负，ADR-0015）｜`side` VARCHAR(10) NOT NULL DEFAULT 'asset'（0002）｜`asset_date` DATE NOT NULL｜`source_evidence_id` FK→screenshot_evidence.id 可空（0003）｜`asset_item_id` FK→asset_items.id（0008；ORM 可空/迁移库物理 NOT NULL）｜`superseded_by` FK→assets.id 自引用（0008，更正链）｜`cost_basis` NUMERIC(15,2) 可空（0012，写时定格冗余）｜`created_at`/`updated_at`
- 索引：`ix_assets_date`、`ix_assets_item`、**部分唯一索引 `uq_assets_item_date_active(asset_item_id, asset_date) WHERE superseded_by IS NULL`**
- 无本表触发器；write-once 由应用层执行（PUT→409、correct＝同日追加＋supersede）。`CHECK (amount >= 0)` 与 `side` 值域由 0024 补齐。
- **受 0026 侧别触发器约束**：`assets_side_matches_class_ins/upd` 要求 `assets.side` 与其 `asset_item` 所属类别的 `side_scope` 同侧（`any` 分支节点不约束）。

**2. `asset_items`** — 资产项＝身份聚合根（持久 ID；改名不改身份；同标的多渠道分条）（0008，v5 重建于 0026）
- 身份列：`id` PK｜`display_name` VARCHAR(100) NOT NULL｜`channel` VARCHAR(100) 可空（**v5 前的渠道自由文本，遗留列**，新代码写 `channel_id`；见 §六-3）｜`status` VARCHAR(10) NOT NULL DEFAULT 'active'（**active｜archived｜draft**，R-37 + ADR-0019 D9）｜`legacy_name` VARCHAR(100) UNIQUE 可空（V4 回填锚）｜`archived_effective_from` DATE 可空（0025，ADR-0017 D2 修订：归档＝自该日起终止计量；archived 且 NULL＝归档日未知，保守剔除）｜`created_at`/`updated_at`
- 分类七维（ADR-0019 D2，从记录级上移到项级）：`asset_class_id` FK→asset_classes.id｜`liquidity_days` INTEGER｜`institution_id` FK→institutions.id｜`owner_id` FK→members.id｜`channel_id` FK→channels.id｜`risk_level` VARCHAR(2)（R1~R5）｜标签走 `asset_item_tags`
- **`owner_id` 是归属的唯一真相源**（ADR-0020 D3）：`asset_item_owner_changes` 只是审计史；写入单点 `services.asset_owner_service.set_item_owner` 保证列与史同事务。
- 约束：`ck_asset_items_status`（三值）｜`ck_asset_items_dims_required`（`status IN ('draft','archived') OR 四维全非空`）｜`ck_asset_items_liquidity_days`（白名单 `0,1,7,30,90,180,365,9999`）｜`ck_asset_items_risk_level`（`R1`~`R5`）｜`uq_asset_items_legacy_name`
- 索引：`ix_asset_items_status`、`ix_asset_items_class`、`ix_asset_items_institution`、`ix_asset_items_owner`
- **触发器** ×2：`asset_items_leaf_class_ins/upd`——被引用的资产类别必须是叶子（有子节点者不得被挂），把「选到中间层」这类错从应用层挪到存储层。

**3. `asset_item_names`** — 显示名史（0008；**0035 扩列**）
- `item_id` FK→asset_items.id｜`name`｜`effective_from` DATE（**0035 起可空：别名行为 NULL**）｜`origin`（`migration|rename|alias`，0035 扩值域）｜`reason`（`correction|evolution|normalization`，**存量 NULL**）｜`effect`（`current|history_backfill`，存量 NULL）｜`reason_note` VARCHAR(200)｜`created_at`
- 约束：`ck_asset_item_names_origin` / `_reason` / `_effect` 三个值域 CHECK，外加 `ck_asset_item_names_alias_date`——`origin='alias'` **必须**无生效日、非别名行**必须**有生效日。
- 索引：`ix_asset_item_names_item`、`ix_asset_item_names_name`；**触发器** ×2（0023 补齐 update/delete 拒绝）。
- 三类行：`migration`/`rename`＝曾经是当前名（带生效日），`alias`＝同一实体的另一个叫法（从未当过当前名、无撤销通道）。`reason`/`effect` 记一次改名为什么发生、影响到哪一层；**NULL 的语义是「当时没记」，不是「没发生」**（ADR-0026）。

**4. `asset_item_attr_history`** — 资产项分类变更史｜**append-only**（0026，ADR-0019 D10）
- `id` PK｜`item_id` FK→asset_items.id｜`dim` VARCHAR(20)（CHECK ∈ `asset_class|liquidity|institution|channel|owner|risk|tag`）｜`old_value`/`new_value` TEXT 可空｜`changed_at` DATETIME｜`changed_by`｜`reason`
- 索引：`ix_item_attr_history_item(item_id, changed_at)`；**触发器** ×2（`_no_update`/`_no_delete`）。
- 存在的理由：分类上移到项级后，「这条记录当时怎么分类」不再可查，故每次变更必须留一行；更正＝追加新行。**注意这张表只记分类，不记归属的「当前值」**——归属当前值在 `asset_items.owner_id`。

**5. `principal_changes`** — 本金口径变更史＝权威真源｜**append-only**（0012）
- `asset_item_id` FK｜`new_amount` NUMERIC(15,2)（**带符号**）｜`effective_date` DATE｜`reason` TEXT NOT NULL｜`evidence_ref` FK 可空｜`origin` TEXT DEFAULT 'external'（external|transfer|lineage）｜`transfer_event_id` 可空（0013 启用）｜`created_at` TEXT
- 索引：`ix_principal_item_eff(asset_item_id, effective_date, id)`；**触发器** `principal_changes_no_update`/`_no_delete`。

**6. `transfer_events`** — 内部买卖转移登记行｜**append-only**（0013）
- `from_item_id`/`to_item_id` FK→asset_items.id｜`amount` NUMERIC(15,2) NOT NULL **恒正（CHECK `amount > 0`）**（方向由 from/to 表达）｜`event_date` DATE｜`reason` TEXT NOT NULL｜`evidence_ref` FK 可空｜`created_at` TEXT
- 索引：`ix_transfer_from(from_item_id, event_date)`、`ix_transfer_to(to_item_id, event_date)`；**触发器** ×2。服务层同事务写两条 `principal_changes`（origin='transfer'）。

**7. `lineage_links`** — 血缘链（前身→后继）｜**append-only、创建即固定**（0014）
- `from_item_id`/`to_item_id` FK｜`link_type` TEXT CHECK ∈ merge|split|revision（**无 rename**）｜`split_ratio` NUMERIC(6,4) 仅 split 携带（(0,1] 服务层校验）｜`note` 可空｜`created_at` TEXT
- 索引：`ix_lineage_from(from_item_id, id)`、`ix_lineage_to(to_item_id, id)`；**触发器** ×2。

**8. `asset_item_owner_changes`** — 归属变更史（**降为审计**，当前值在 `asset_items.owner_id`）｜**append-only**（0016，ADR-0020 D3 修订）
- `asset_item_id` FK｜`owner_kind` TEXT CHECK ∈ member|family_pool（**`family_pool` 已退役**，仅历史行可能残留；写入路径一律 422）｜`owner_id` FK→**members.id** 可空（0027 改指，原为 attributes.id）｜`effective_date` DATE｜`reason`｜`origin` TEXT DEFAULT 'manual' CHECK ∈ manual|migration｜`created_at` TEXT
- 索引：`ix_item_owner_item_eff(asset_item_id, effective_date, id)`、`ix_item_owner_owner`、**部分唯一 `uq_item_owner_migration(asset_item_id, effective_date) WHERE origin='migration'`**
- **触发器** ×2；约束 `ck_item_owner_pair`（kind 与 owner_id 联动）。

**9. `audit_log`** — 审计日志（不变量 8 承载物）｜**append-only**（0009）
- `occurred_at` TEXT｜`actor`（owner|system，0024 补 CHECK）｜`entity`｜`action`｜`row_id`（非 FK）｜`before_json`/`after_json`｜`evidence_ref` FK 可空｜`request_id`｜`idempotency_key`（0010）｜`prev_hash` VARCHAR(64)（0018 起全链化）
- 索引：`ix_audit_entity_row`、`ix_audit_occurred_at`、`ix_audit_evidence_ref`、`ix_audit_request_id`（后两个由 0023 补）
- **触发器** `audit_log_no_update`/`_no_delete`；ORM 无 relationship。裸 INSERT 会留 `unchained_row` 断点——迁移/回填一律不直写本表（0028 即因此只在日志记数）。

**10. `audit_chain_state`** — 审计链单行状态（CAS 锚）（0018）
- `id`｜`algorithm`（sha256.v1）｜`last_id`｜`last_hash`｜`row_count`｜`updated_at`
- 无触发器；由 `audit_chain.append/append_sqlite` 以 CAS 方式推进，链与状态不符时启动期拒接。

**11. `audit_chain_seals`** — 审计日封存（0018）
- `sealed_date`｜`sealed_at`｜`algorithm`｜`last_id`｜`last_hash`｜`row_count`｜`prev_seal_hash`｜`seal_hash`
- **触发器** ×2（不可改删）。

### 凭证区

**12. `screenshot_evidence`** — 凭证元数据（原图+SHA256+来源组）
- `source_group_id` **FK→source_groups.id NOT NULL**（0029 起；此前是 `source_group` VARCHAR(50) 存 slug 快照、无外键）｜`file_path`｜`file_sha256`｜`original_filename`（0032 起脏临时名 `shot_upload_*` 已置 NULL，只留真实客户端原名）｜`data_date`｜`supplement_rows`（0006，只写不读）｜`engine`（0007，ocr|ai，只写不读）｜`created_at`｜`image_meta`（0032，JSON 白名单：`width/height/bytes/format` + `exif.DateTimeOriginal/Make/Model/Software/Orientation`，**不存 GPS**，ADR-0022）｜`recognition_json`（0032，识别快照 JSON，`schema:1` 的 `recognized`/`adopted` 两段对照，体积上限 1 MiB，ADR-0023）（**11 列**；`anchor_amount` 已于 0030 物理删除，见 ADR-0021）
- `image_meta` / `recognition_json` 均可空：读不出元数据 → NULL 且不阻断存证；未识别过 → NULL。二者**只写不改**（同组同 sha 复用既有行时不重写，见 spec D5）。
- 索引 `ix_screenshot_evidence_group_date(source_group_id, data_date)`；**唯一索引 `ux_screenshot_evidence_sha_group(file_sha256, source_group_id)`**（0033，ADR-0024：同一张图在同一来源组只存一份，跨组同图各存一份、各带自己的 `sg<组id>` 名）；约束 `ck_evidence_engine`。
- 行数：spec 冻结时 28 行 / 23 个不同 sha（5 组「同 sha 同组两行」）→ 0033 后 23 行；2026-11-06 副本演练实测 34 行 / 31 个不同 sha（3 组）→ 31 行（另 2 组的零引用行已在 spec 冻结后被清掉）。
- 对外 API 仍以 slug 字符串收发（读路径 join 取当前 slug），组改名不再需要任何级联代码——**组身份是 id，不是名字**。
- 被 `assets`/`audit_log`/`principal_changes`/`transfer_events` 四处 FK 引用；永久保留、无删除入口。

### 参考区

**13. `asset_classes`** — 资产类别树（ADR-0019 D1/D8）＝取代旧 `attributes(type='fund_type')`
- `id` PK｜`code` VARCHAR(10)（稳定标识 A1/B3…，UNIQUE）｜`name` VARCHAR(50)｜`parent_id` 自引用可空｜`level` INTEGER（CHECK ≥1）｜`side_scope` VARCHAR(10)（CHECK ∈ `asset|liability|any`；`any` 只出现在分支节点，叶子恒为具体一侧）
- 约束：`uq_asset_classes_code`、`uq_asset_classes_parent_name`、`ck_asset_classes_side_scope`、`ck_asset_classes_level`；索引 `ix_asset_classes_parent`
- 叶子性由 `asset_items_leaf_class_ins/upd` 触发器守；名义上「一棵树覆盖资产与负债两侧」。

**14. `institutions`** — 金融机构/自持主体（ADR-0019 D3）＝取代旧 `attributes(type='account')` 的机构语义
- `id` PK｜`name` VARCHAR(50) UNIQUE｜`kind` VARCHAR(20)（CHECK ∈ `bank|broker|fund|insurer|gov|platform|other`；**这是机构类型，不是资产类别**）｜`active` BOOLEAN DEFAULT 1
- 「自持」「个人」也在这张表（kind=`other`），因为两者都是资产项的第一段主语。

**15. `channels`** — 触达渠道（ADR-0019 D3）＝机构的下级入口（App/网点/直销）
- `id` PK｜`name` VARCHAR(50) UNIQUE｜`active` BOOLEAN DEFAULT 1
- 与机构拆开是因为「同一机构多渠道＝多条资产项」（拍板 7）。存量全部归到机构层，故 v5 迁移后本表为空、`asset_items.channel_id` 可空。

**16. `members`** — 家庭成员/归属主体（ADR-0019 D6）＝取代旧 `attributes(type='owner')` 的真人姓名行
- `id` PK｜`name` VARCHAR(50) UNIQUE｜`role` VARCHAR(20)（CHECK ∈ `self|spouse|joint|child|nominee`）｜`active` BOOLEAN DEFAULT 1
- 只存角色不存姓名：旧库把成员姓名直接写进属性名，进不了版本库、换设备即失。

**17. `tags`** — 标签字典（ADR-0019 D7）
- `id` PK｜`name` VARCHAR(50) UNIQUE｜`kind` VARCHAR(20)（CHECK ∈ `controlled|free`：系统预置 vs 设置页显式创建）

**18. `asset_item_tags`** — 资产项 × 标签（多值维度）
- `item_id` FK→asset_items.id + `tag_id` FK→tags.id（复合 PK）；索引 `ix_asset_item_tags_tag`
- 标签挂项不挂记录：一次标注覆盖全历史。

**19. `source_groups`** — 截图来源组（`slug`/`label` UNIQUE；`rules` JSON；`enabled`/`sort_order`）
**20. `source_group_accounts`** — 来源组↔机构 M:N（复合 PK；`attribute_id` 于 0027 改指 `institutions.id`，按名字映射，查不到同名机构即中止迁移）
**21. `allocation_targets`** — 配置目标（dimension/target_id/parent 层级/target_percent/`alert_threshold` 0004/`alert_threshold_red`+`relative_threshold` 0017）
- `dimension` 值域（ADR-0019 后）：`asset_class|liquidity|institution|channel|owner|risk|risk_level|asset_name|tag`（旧值 `fund_type`/`liquidity_rating`/`account` 不再接受——它们指向已废弃的 attributes 节点 id 空间）
- `target_id` 为 **VARCHAR 无 FK**；`parent_id` 有索引（`ix_allocation_targets_parent`）；`target_percent`/阈值均带 CHECK（0024/0026）。
**22. `allocation_target_revisions`** — 阈值/目标变更史｜**append-only**（0020）
- `allocation_target_id`｜`op`｜`dimension`｜`target_id`｜`parent_id`｜`target_percent`｜`alert_threshold`｜`alert_threshold_red`｜`relative_threshold`｜`effective_date`｜`reason`｜`origin`｜`created_at`；CHECK + **触发器** ×2。
**23. `alert_rules`** — 预警规则（维度/周期/比较/阈值/方向）；`dimension` 走与 `allocation_targets` 同构的别名归一（`risk`→`risk_level`），旧维度值只存在于历史行。
**24. `expense_categories`** — 支出分类（`name` UNIQUE）
**25. `fixed_expense_templates`** — 固定支出模板（cycle 月/季/年；`category_id` 引用 SET NULL，`account_id` 于 0027 改指 `institutions.id`）
**26. `llm_providers`** — LLM 供应商（kind/base_url/api_key/…；出口脱敏）；`api_key` 明文列，备份与导出需注意。
- **0034 增四列**（均可空、无默认值）：`last_tested_at`｜`last_test_ok`｜`last_test_message`（截断 300 字）｜`last_test_elapsed_ms`——存**最近一次**测试结论，不存历史。
- **`NULL`＝从未测试，`false`＝测过且失败**，两者不可混（ADR-0025）；`base_url`/`model`/`api_key` 的**值**变更即清空四列，`label`/`sort_order`/`enabled` 不变。

**46. `parser_vocabularies`** — 解析词表增量覆盖（parser-rules P1，0031）；逻辑归属「参考区」，编号顺延
- `key` PK（VARCHAR）｜`add_items` TEXT（JSON 数组）｜`remove_items` TEXT（JSON 数组）｜`updated_at`
- **生效词表 ＝ `(builtin[key] ∪ add) − remove`**：出厂值永远是代码常量（`services/parser_rules.py`），本表只存增量，故「改坏」可 `reset` 回退、出厂词不会被永久抹除。
- 7 个 key：`amount_headers` / `ignore_words` / `total_labels` / `name_exclude` / `profit_zone` / `row_close_tokens` / `noise_patterns`。无行 = 全出厂值。
- 增删词必须带 `reason` 并写 `audit_log`（`action=parser_vocabulary_update|reset`）。

**47. `source_group_learned_rules`** — 来源组教学规则（parser-rules P1，0031）
- `id` PK｜`source_group_id` FK→source_groups.id NOT NULL｜`rule_type` CHECK ∈ `rename|ignore|exclude`｜`match`｜`replace`（仅 rename）｜`note` 可空｜`sort_order`（rename 靠它保序级联）｜`origin` CHECK ∈ `manual|import|migration`｜`hit_count`（P1 建列不累加，P2 启用）｜`last_hit_at`｜`created_at`/`updated_at`
- 唯一索引 `uq_sglr_group_type_match_replace`：`(source_group_id, rule_type, match, COALESCE("replace",''))`——用表达式是为了让「replace 都为空」的两条互相冲突（普通唯一约束下 SQLite 视为 NULL 互不相等，那正是要修的病）。
- 三型语义**刻意不统一**：`rename` 子串替换可按列表序级联、`ignore` 子串命中丢行、`exclude` 严格全等不录入。
- 0031 前这些规则存在 `source_groups.rules` JSON 的三键里；迁移后 JSON 只剩 `layout`/`amount_header`/`amount_col`/`ignore_words`。

**48. `asset_item_rename_tokens`** — 改名确认令牌（asset-name-identity，0035，ADR-0026）
- `id` PK｜`token` VARCHAR(64) **唯一**｜`item_id` FK→asset_items.id｜`payload_json` TEXT（item/新名/reason/effect/note/影响面哈希，规范化 JSON）｜`impact_hash` VARCHAR(64)｜`expires_at`｜`used_at` 可空｜`created_at`；索引 `ix_asset_item_rename_tokens_item(item_id)`。
- 一次性、10 分钟 TTL：`/rename/preview` 落一行并返回 token，`/rename` 校验「未用 + 未过期 + 载荷逐字段一致 + 影响面哈希未变」后置 `used_at`。
- 存在的理由：回写历史行不可逆，「先看影响面再按确认」必须是服务端强制，而不是前端对话框的自觉。**不是业务表**，可随时按 `created_at` 清理历史行。

### 规划区

**27. `goals`** — 目标（0015）
- `id`｜`name`｜`scope_kind`｜`scope_owner_id`（**0027 改指 `members.id`**，原为 attributes.id）｜`is_primary`｜`created_at`；约束 `ck_goals_*` + **部分唯一 `uq_goals_primary`**；`scope_owner_id` **无索引**。
**28. `goal_versions`** — 目标版本（不可变；active 唯一）（0015）
- `id`｜`goal_id`｜`version_no`｜`status`｜`target_amount`｜`target_date`｜`target_rate_annual`｜`risk_profile`｜`life_stage`｜`reason`｜`effective_from`｜`criterion_json`｜`created_at`｜`activated_at`｜`archived_at`
- 约束 `ck_goal_versions_*`（10 条）+ **部分唯一 `uq_goal_versions_active`**；无触发器（不可变由应用层保证）。

### 建议隔离区（ADR-0013/0014：与账本区零反向 FK）

**29. `advisor_info_sets`** — 信息集快照（`as_of`/goal 绑定/`payload_sha256`/`meta_json`）；**触发器** ×2
**30. `advisor_info_entries`** — 信息集条目（`key`/`kind`/`value`/`value_scaled`/`trust`/`source`/`source_ref`）；**触发器** ×2
**31. `advisor_suggestions`** — 建议主表（七态状态机 + `dedupe_key` UNIQUE + gate/prompt/provider 元数据）；`ck_advisor_suggestion_status` + **内容写一次触发器**
**32. `advisor_suggestion_items`** — 建议条目（`subject_kind`/`subject_ref`/`direction`/rationale 双列）；**触发器** ×2
**33. `advisor_decisions`** — 采纳/驳回裁决｜append-only（`action`/`execution_status`/`decided_at`）；**部分唯一 `uq_advisor_terminal_decision`** + **触发器** ×2
**34. `advisor_jobs`** — 异步生成任务（`status`/`stage`/`attempts`/`error`）；CHECK，无触发器
**35. `advisor_settings`** — 单行配置（`auto_enabled`/`cooldown_days`/`default_horizon`）；CHECK，种子 1 行
**36. `advisor_reviews`** — 复盘定格（`snapshot_json`+`snapshot_sha256` write-once、closed 冻结、禁 DELETE）；**触发器** ×3
**37. `advisor_review_suggestions`** — 复盘↔建议关联；**触发器** ×2
**38. `advisor_review_proposals`** — 复盘提案（`kind`/`payload_json`/`status`/`applied_*`）；**部分唯一 `uq_advisor_review_proposal_content`** + **触发器** ×3

### 会话/任务区与流水

**39. `expenses`** — 支出流水（**不参与净值**；`UNIQUE(template_id, period)` 幂等生成；手动记录双 NULL）；`account_id` 于 0027 改指 `institutions.id`；amount/expense_type 带 0024 CHECK
**40. `import_batches`** — 导入批次记录（`filename`/`record_count`/`status`）；**无 `import_batch_id` 外键**，与 `assets` 无结构关联
**41. `export_history`** — 导出历史（`export_type`/`filename`/`file_size`/`file_path`/`rule_name`）；行不随文件删除而清理 → 维护脚本 `scripts/db_cleanup_p0.py` 负责裁剪
**42. `auto_export_rules`** — 自动导出规则（cron/format/template/`last_run_at`/`next_run_at`）
**43. `idempotency_keys`**（无 ORM，0010）— 幂等去重（key PK/scope/body_hash/status/response；TTL 7 天）
**44. `preview_jobs`**（无 ORM，0011）— 预览任务持久底座（payload_json/进度/终态；done>7 天清、queued/running 永不清）

### 系统记账

**45. `schema_version`**（runner 自建）— 迁移记账（`version` PK/`name`/`baseline`/`applied_at`）；`name` 由 `fn[4:-3]` 派生，带前导下划线（如 `_drop_v1_tables`），属命名瑕疵。

## 三、迁移标注

| 迁移 | 内容 |
|---|---|
| 0001~0007（CLAIM=True，v4 库首跑仅认领） | V1 五表 DROP、`assets.side` 回填、`source_evidence_id`、`alert_threshold`、`remind_enabled`、`supplement_rows`、`engine` |
| **0008** | 身份层：`asset_items`/`asset_item_names` + **assets 12 步重建**（item FK/superseded/部分唯一索引；id 原值保留；五重校验断言，失败回滚中止） |
| **0009** | `audit_log` + 双 append-only 触发器 |
| **0010** | `idempotency_keys` + audit 加 `idempotency_key` 列 |
| **0011** | `preview_jobs` |
| **0012** | `principal_changes` + 双触发器；assets 加 `cost_basis` |
| **0013** | `transfer_events` + 双触发器 + `amount > 0` CHECK + 双索引 |
| **0014** | `lineage_links` + 双触发器 + `link_type` CHECK |
| **0015** | `goals` + `goal_versions`（scope/primary/version 状态机约束） |
| **0016** | `asset_item_owner_changes` + 双触发器 + 三条 CHECK + `origin='migration'` 部分唯一；历史 owner 回填 |
| **0017** | `allocation_targets` 加 `alert_threshold_red`/`relative_threshold`（可空，NULL 走代码兜底） |
| **0018** | `audit_chain_state`/`audit_chain_seals` + `prev_hash` 链化回填（临时摘触发器重建 audit_log） |
| **0019** | advisor 七表：info_sets/entries/suggestions/suggestion_items/decisions/jobs/settings |
| **0020** | `allocation_target_revisions` + 双触发器 |
| **0021** | 复盘三表：advisor_reviews/review_suggestions/review_proposals |
| **0022** | 纯数据回填：记录级 owner → item 级归属史（幂等、无候选空跑） |
| **0023** | 只做加法：19 个读写路径索引 + `asset_item_names` 双 append-only 触发器（补 D3/D6） |
| **0024** | 值域约束：12 张表重建后补 19 条 CHECK（`amount >= 0`、`side`、`status`、`origin`、`actor`、`attributes.type`、`engine`、目标值域、支出类型/金额、模板周期、转移/血缘自指禁止）；只补约束、不删列、不动行 |
| **0025** | `asset_items.archived_effective_from`（ADR-0017 D2 修订：归档＝自该日终止计量） |
| **0026** | **v5 分类重构主体（ADR-0019）**：新建 7 表（`asset_classes`/`institutions`/`channels`/`members`/`tags`/`asset_item_tags`/`asset_item_attr_history`）；`asset_items` 12 步重建加 6 列 + 4 条 CHECK（`status` 值域扩至含 `draft`，新增 `dims_required`/`liquidity_days`/`risk_level`）+ 4 个索引；记录级 EAV 上移到项级（15,892 行 → 609 行）；**6 个新触发器**（类别叶子性 ×2、side 与 side_scope 同侧 ×2、分类变更史 append-only ×2）。**不动 `assets` 任何行** |
| **0027** | **拆除 EAV 两表（ADR-0019 D11）**：`attributes`/`asset_attributes` 物理删除；4 处外键改指（`source_group_accounts.attribute_id`→`institutions.id`、`goals.scope_owner_id`/`asset_item_owner_changes.owner_id`→`members.id`、`expenses.account_id`/`fixed_expense_templates.account_id`→`institutions.id`），一律 12 步重建 + 名字映射校验 |
| **0028** | 纯数据回填：归属史最新一行 → `asset_items.owner_id`（单向、幂等、`IS NULL` 守卫；`family_pool` 保持 NULL；脏值不猜只记日志） |
| **0031** | 教学规则拆表（L3）：从 `source_groups.rules` JSON 搬出 `rename`/`ignore`/`exclude` 三键进 `source_group_learned_rules`，并建立 `parser_vocabularies`；顺带清理四个后端从不读取的历史死键（`name_col`/`total_header`/`amount_suffix`/`anchor_summary`）。守卫：缺 `source_groups` → skipped；坏 JSON 中止；分类型行数对账；幂等 |
| **0029** | 凭证来源组收口（L2）：`screenshot_evidence` 12 步重建——`source_group` VARCHAR 快照 → `source_group_id` **NOT NULL 外键**，索引改建在 `(source_group_id, data_date)`；前置悬空 >0 即 fail-fast 中止，重建后校验 `foreign_key_check` 空 / `integrity_check=ok` / NULL 归零。组改名从此不需要任何级联代码 |
| **0030** | 删除死字段（L2，ADR-0021）：`screenshot_evidence.anchor_amount` 物理删除——`screenshot_evidence` 12 步重建，重建后 **9 列**，索引 `ix_screenshot_evidence_group_date(source_group_id, data_date)` 与 `ck_evidence_engine` 原样保留；守卫：表不存在 → skipped、无该列 → noop、无 `source_group_id` 或父表（桩表形状）→ skipped、`PRAGMA foreign_keys=ON` → 中止；重建后校验列集合/索引逐字、`foreign_key_check` 空、`integrity_check=ok`、行数对账。**运行库已于 2026-11-05 执行** |
| **0032** | 凭证两列 + 清脏原始名（L2，ADR-0022/0023）：`screenshot_evidence` 仅 `ADD COLUMN`（**不做表重建**）——`image_meta TEXT`（原图白名单 JSON）、`recognition_json TEXT`（识别快照）；并把 `original_filename LIKE 'shot_upload_%'` 的临时名置 NULL（运行库命中 23 条，真名 5 条不动，**不搬文件、不改 `file_path`**）。守卫：表不存在 → skipped、缺基准列（桩表形状）→ skipped、**逐列判定**（两列都在 → 不加列；只缺一列 → 只补那列）、`UPDATE` 每次跑并报受影响行数（二跑 0 行 → noop）、加列后列集合逐字校验 11 列 + `foreign_key_check` 空 + `integrity_check=ok`。**副本演练通过；运行库已于 2026-11-05 执行（随容器重建触发，实测脏名清空 23 条）** |
| **0033** | 凭证按来源组去重 + 复合唯一索引（L2，ADR-0024）：删「同组内还有同 sha 的另一行」且「`assets.source_evidence_id` / `audit_log.evidence_ref` / `principal_changes.evidence_ref` / `transfer_events.evidence_ref` 四类引用全为 0」的行（每组保留引用最多者，并列取 id 最小；spec 口径 28 → 23 行，2026-11-06 副本演练实测 34 → 31 行，删 ids 4/7/17），再建 `CREATE UNIQUE INDEX IF NOT EXISTS ux_screenshot_evidence_sha_group ON screenshot_evidence(file_sha256, source_group_id)`。守卫：表不存在 / 缺 `id`·`file_sha256`·`source_group_id`（桩表形状）→ skipped；索引已在且无重复行 → noop；**删完仍有同组同 sha（多行都被引用）→ 抛错点名**（不把 `UNIQUE constraint failed` 甩给启动链）；建索引后校验 `index_list` 的 `unique=1` + `index_info` 列序逐字 + `foreign_key_check` 空 + `integrity_check=ok`。**只删行、不删文件**（留下的 5 个孤儿由 `--report-orphans` 报告、业主备份后删除）。**副本演练通过；运行库待业主停服窗口执行** |
| **0035** | 名史扩 reason/effect/别名 + 改名令牌表（L2，ADR-0026）：`asset_item_names` **12 步重建**——`origin` CHECK 扩 `alias`、`effective_from` 改可空、加 `reason`/`effect`/`reason_note` 三列与两个值域 CHECK + `ck_asset_item_names_alias_date`；`INSERT … SELECT` 只搬原 6 列（存量 146 行原样，原因列 NULL），重建 `ix_asset_item_names_item`/`_name` 与 `asset_item_names_no_update`/`_no_delete` 两触发器；另建 `asset_item_rename_tokens` + `ix_asset_item_rename_tokens_item`。守卫：表不存在 → `skipped`（新库由 `create_all` 建新形状）、已是新形状 → `noop`（仍补建令牌表）、重建后校验行数守恒 + 9 列形状逐字 + DDL 含 `'alias'` + 索引与触发器齐全 + `integrity_check=ok`，任一不符即抛错中止启动。**副本演练通过（146→146 行、二跑 noop、UPDATE 仍被拦截）**；运行库随本轮部署执行 |

## 四、关系图（FK 方向 A→B 读作 A 引用 B）

```
asset_item_names ─→ asset_items ◄─(asset_item_id)─ assets ─(source_evidence_id)→ screenshot_evidence
                        ▲    │                       ↑ superseded_by(自引用)      ▲ evidence_ref
                        │    └─(asset_class_id)──→ asset_classes (自引用 parent)
                        │    └─(institution_id)──→ institutions ◄── source_group_accounts ──→ source_groups
                        │    └─(channel_id)──────→ channels
                        │    └─(owner_id)────────→ members ◄── goals.scope_owner_id
                        │    └─ asset_item_tags ──→ tags
                        ├── principal_changes
                        ├── transfer_events(from/to)
                        ├── lineage_links(from/to)
                        ├── asset_item_attr_history
                        └── asset_item_owner_changes ─→ members(owner_id)
assets ◄── assets_side_matches_class 触发器（side 必须与类别 side_scope 同侧）
goals ─→ goal_versions ◄─ advisor_info_sets / advisor_suggestions / advisor_reviews
advisor_suggestions ◄─ advisor_suggestion_items / advisor_decisions / advisor_jobs / advisor_review_suggestions
expenses / fixed_expense_templates → expense_categories / institutions
隔离设施：audit_log（无 relationship）、audit_chain_state/seals、idempotency_keys、preview_jobs、schema_version
```

要点：账本写路径只有 ledger 单点（`get_or_create_item` 等）；append-only 表无应用层改删端口（更正＝追加）；建议隔离区不得被反向 FK 指入写路径；**分类七维的基数由 schema 保证（列 + CHECK + 触发器），不再靠应用层规矩**——这是 v5 重构的全部要点。

## 五、不变量速查（硬约束清单）

| 约束 | 载体 | 强制层 |
|---|---|---|
| 39 个触发器 / 19 张表：不可改删 | `principal_changes`(2)、`transfer_events`(2)、`lineage_links`(2)、`asset_item_owner_changes`(2)、`asset_item_names`(2)、`asset_item_attr_history`(2, 0026)、`assets`(2, 0026)、`asset_items`(2, 0026)、`audit_log`(2)、`audit_chain_seals`(2)、`allocation_target_revisions`(2)、advisor 8 表(17) | 存储层 |
| 同日同资产项仅一行 active | 部分唯一索引 | 存储层（0008） |
| 唯一 active 目标版本 / 唯一 primary 目标 | 部分唯一索引 | 存储层（0015） |
| 身份锚稳定 | `legacy_name` UNIQUE（NULL 可多） | 存储层 |
| 生成记录幂等 | `UNIQUE(template_id, period)` | 存储层 |
| 快照金额/日期/方向 write-once | PUT→409、correct＝追加+supersede | 应用层（无触发器） |
| 金额非负（0＝该日余额为零） | `CHECK (amount >= 0)`（0024，ADR-0015） | 存储层 |
| 值域约束 18 条 / 11 表 | `assets.side`、`asset_items.status`、`asset_item_names.origin`、`principal_changes.origin`、`audit_log.actor`、`screenshot_evidence.engine`、目标 dimension/percent/threshold、`expenses.expense_type/amount`、模板 cycle/amount、转移/血缘 `from<>to`（原 19 条含 `attributes.type`，随 0027 删表消失；全库命名 CHECK 现共 59 条） | 存储层（0024） |
| **分类维度基数** | `asset_items` 四维 NOT NULL（draft/archived 豁免）+ `liquidity_days`/`risk_level` 白名单 + 类别叶子性触发器 + `side` 与 `side_scope` 同侧触发器 | 存储层（0026） |
| **归属单一真相源** | `asset_items.owner_id`（列），`asset_item_owner_changes` 仅审计 | 存储层（约束 `ck_item_owner_pair`）+ 应用层单点写入 |
| 凭证不可删 | 无删除入口＋备份只增不删 | 规范/应用层 |
| 迁移 fail-fast | 每脚本一事务＋记账＋前置备份 | runner |

## 六、已知差异与待办（2026-11-04 复核）

1. **`assets.asset_item_id` 可空性双标准**：迁移库物理 NOT NULL，create_all 路径保持可空（严格度不一致，待统一）。0026 明确「不动 assets」，故本项未随 v5 消除。
2. **金额以 REAL/INTEGER 落盘**：与「字符串序列化 Decimal」约定不符，当前子分残差 ≤1.5e-8，属已知精度缺口（v5 重建未改变落盘方式）。
3. **`asset_items.channel` 遗留列**：v5 前渠道是自由文本列，现新增 `channel_id` 外键而旧列仍保留（0026 未删）。新代码只写 `channel_id`；旧列是否为历史快照或应删除，尚未裁决 → 登记为待办。
4. **`asset_classes.side_scope='any'` 只约束分支**：叶子恒为 `asset`/`liability`，`any` 出现在叶子会让 side 触发器失去判据，目前仅靠 0026 的 DDL 约定与文档约束，无存储层强制。
5. ~~`asset_item_names` 标称 append-only 但无触发器~~ → **0023 已补齐双触发器**。
6. ~~20 个 FK 列无前导索引~~ → **0023 已补 19 个**（余下由既有复合索引的前导列覆盖）。
7. ~~枚举值域普遍无 CHECK~~ → **0024 已补**；v5 又新增 9 条分类相关 CHECK（`asset_classes` 2、`institutions` 1、`members` 1、`tags` 1、`asset_item_attr_history` 1、`asset_items` 3）。
8. **`allocation_targets.target_id` 仍无 FK**（VARCHAR，按 dimension 解释为 id 或 name）；值域 CHECK 已随 ADR-0019 换新，但存量旧维度字符串若原地升级会变成「读得出、写不进」（读路径靠别名折叠，写路径 422）→ **0026/0027 只服务空库重建路径**（见 `.scratch/db-rebuild/spec.md` §7 前置约束）。
9. 备份保留策略只覆盖手动备份：`pre_migration`/`before_import` bundle 走 `prune=False`，需周期性裁剪（`scripts/db_cleanup_p0.py backups`）。
10. 已完成的维护动作记在 `.scratch/db-audit/audit-2026-10-29.md`（不入库的本地记录）。
11. **归属列有第二个写入旁路（离线重建脚本）**：`scripts/rebuild/s2_plan_clean.py` / `s4_rebuild_db.py` 按列直写 `asset_items.owner_id` 与 `asset_item_owner_changes`，不经过 `asset_owner_service.append_change`（不推状态机、不写 `audit_log`）。**运行时**写入仍是单点，离线重建是明确例外；重建产物的归属史全部为 `origin='migration'` / `owner_kind='member'`（2026-11-04 实库复核，无 `family_pool` 残留）。
12. **重建产物没有分类变更史基线**：切库后的库里 `asset_item_attr_history` 为 **0 行**——旧库无此概念，s4 也不导入。首次在 UI 改分类时才会落下第一行，此前「初始分类从哪来」没有审计记录（可接受，但审计链从第一次改动才开始）。
13. **迁移 0028 对重建产物是 no-op**：s4 顺序为「建库 → 迁移 → 导入」，0028 在空表上回填 0 行；它只服务存量库升级路径。重建自己按列写归属并与史保持一致，因此不需要它补齐。
14. **重建曾丢「凭证组」配置（已修）**：D8 原把 `source_groups` 归入「纯配置不迁」，但它同时是 `screenshot_evidence.source_group` 的事实字典、OCR 识别规则（`rules` JSON）的宿主、组↔机构账户名映射的父表——跳过会导致历史凭证来源组悬空、识别规则不可重建。现由 `scripts/rebuild/s4b_restore_config.py` 从旧库随迁（按 slug 对齐、只补空字段、机构名精确匹配、默认 dry-run），runbook §7 第 4 步执行；2026-11-05 已在运行库上恢复并复核（凭证悬空归零）。**重建收尾必须跑它，否则会再次丢配置。**

## 汇总项与批次（0037 / 0038）

| # | 表 | 要点 |
|---|---|---|
| 49 | `item_compositions` | 父 — 子边；`UNIQUE(child_item_id)` 保证一子一父；`CHECK(parent_item_id <> child_item_id)`；`ix_item_compositions_parent` |
| 50 | `screenshot_batches` | 一次多图录入的容器；`status ∈ open/partial/finalized/abandoned`；`expected_members` 用于判定成员是否到齐 |
| 51 | `screenshot_batch_members` | `UNIQUE(batch_id, evidence_id)`；`status ∈ pending/applied/failed` |
| 52 | `screenshot_reconciliations` | 一次核对的结论：父项/父行锚、`aggregate_amount`、`child_amount_sum`、`diff`、`status ∈ matched/accepted_mismatch`、`accepted_reason`；`parent_evidence_id`/`parent_row_index` **可空**（手选既有项作父时无行锚） |
| 53 | `screenshot_reconciliation_children` | 该次核对的子项明细（`item_id` 可空） |

迁移：`0037_item_compositions_and_batches`（建上述五表，零 DML，三态幂等）、`0038_learned_rules_aggregate_type`（来源组学习规则的 `aggregate` 类，表重建）。

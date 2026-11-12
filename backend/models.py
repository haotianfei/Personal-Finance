from datetime import date, datetime
from sqlalchemy import (
    Column, Integer, String, Date, DateTime, Numeric, ForeignKey, Index, UniqueConstraint, Boolean, Text, text,
    CheckConstraint,
)
from sqlalchemy.orm import relationship
from database import Base


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    filename = Column(String(255), nullable=False)
    record_count = Column(Integer, nullable=False, default=0)
    imported_at = Column(DateTime, nullable=False, default=datetime.now)
    status = Column(String(20), nullable=False, default="success")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    dimension = Column(String(50), nullable=False)
    target_id = Column(String(100), nullable=True)
    period_type = Column(String(20), nullable=False)
    compare_type = Column(String(20), nullable=False)
    compare_period = Column(String(50), nullable=True)
    amount_threshold = Column(Numeric(15, 2), nullable=True)
    percent_threshold = Column(Numeric(5, 2), nullable=True)
    direction = Column(String(10), default="both")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class ExportHistory(Base):
    """导出历史记录"""
    __tablename__ = "export_history"
    __table_args__ = (
        Index("ix_export_history_export_time", "export_time"),
        Index("ix_export_history_export_type", "export_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    export_time = Column(DateTime, nullable=False, default=datetime.now)
    export_type = Column(String(20), nullable=False)  # manual, auto
    filename = Column(String(255), nullable=False)
    file_size = Column(Integer, nullable=True)
    operator = Column(String(100), nullable=True)  # 操作人，自动导出时为 null
    rule_name = Column(String(100), nullable=True)  # 自动导出时的规则名称
    file_path = Column(String(500), nullable=True)  # 文件存储路径
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AutoExportRule(Base):
    """自动导出规则"""
    __tablename__ = "auto_export_rules"
    __table_args__ = (
        Index("ix_auto_export_rules_is_active", "is_active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    cron_expression = Column(String(100), nullable=False)
    export_format = Column(String(20), nullable=False, default="csv")  # csv, json
    filename_template = Column(String(255), nullable=True)  # 文件名模板
    is_active = Column(Boolean, default=True)
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AllocationTarget(Base):
    __tablename__ = "allocation_targets"
    __table_args__ = (
        Index("ix_allocation_targets_parent", "parent_id"),
        CheckConstraint(
            # ADR-0019：维度值域随分类重构更换。旧值（fund_type/liquidity_rating/account）
            # 不再接受——它们指向已废弃的 attributes 节点 id，与新结构的 id 空间无关。
            # risk 与 risk_level 同义（部分老调用方用 risk），两者都是新名。
            "dimension IN ('asset_class','liquidity','institution','channel',"
            "'owner','risk','risk_level','asset_name','tag')",
            name="ck_targets_dimension"),
        CheckConstraint("target_percent BETWEEN 0 AND 100",
                        name="ck_targets_percent"),
        CheckConstraint("alert_threshold BETWEEN 0 AND 100",
                        name="ck_targets_alert"),
        CheckConstraint(
            "alert_threshold_red IS NULL OR alert_threshold_red BETWEEN 0 AND 100",
            name="ck_targets_alert_red"),
        CheckConstraint(
            "relative_threshold IS NULL OR relative_threshold BETWEEN 0 AND 100",
            name="ck_targets_relative"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dimension = Column(String(50), nullable=False)  # fund_type, liquidity_rating, account
    target_id = Column(String(100), nullable=False)  # 类型ID、评级名称、账户ID
    parent_id = Column(Integer, ForeignKey("allocation_targets.id"), nullable=True)
    target_percent = Column(Numeric(5, 2), nullable=False)  # 目标比例 0-100
    alert_threshold = Column(Numeric(5, 2), nullable=False, default=5.00)  # 黄阈值·绝对pp，默认5%
    # 三期-2（ADR-0012 D7，迁移 0017）：NULL＝代码兜底
    alert_threshold_red = Column(Numeric(5, 2), nullable=True)  # 红阈值·绝对pp；NULL=2×黄
    relative_threshold = Column(Numeric(5, 2), nullable=True)   # 黄阈值·相对%；NULL=25
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    parent = relationship("AllocationTarget", remote_side=[id], backref="children")


class AllocationTargetRevision(Base):
    """配置目标阈值史（三期-4 W1，ADR-0014 D4/D5；迁移 0020）——append-only。

    与 `allocation_targets` 同名列，`band_service.resolve_band(revision)` 可直接
    鸭子类型复用。**不建 FK**：历史须存活于目标行删除之后；写侧由
    `allocation_service` 单点追加（不在目标表上建触发器）。
    解析：`effective_date <= as_of` 的最新一行（同日按 created_at DESC, id DESC）。
    """
    __tablename__ = "allocation_target_revisions"
    __table_args__ = (
        CheckConstraint("op IN ('create','update','delete','backfill')",
                        name="ck_atr_op"),
        CheckConstraint("origin IN ('manual','migration')", name="ck_atr_origin"),
        Index("ix_atr_target_eff", "allocation_target_id", "effective_date", "id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    allocation_target_id = Column(Integer, nullable=False)   # 无 FK（史须存活）
    op = Column(Text, nullable=False)                       # create/update/delete/backfill
    dimension = Column(Text, nullable=False)
    target_id = Column(Text, nullable=False)
    parent_id = Column(Integer, nullable=True)
    target_percent = Column(Numeric(5, 2), nullable=False)
    alert_threshold = Column(Numeric(5, 2), nullable=False)
    alert_threshold_red = Column(Numeric(5, 2), nullable=True)
    relative_threshold = Column(Numeric(5, 2), nullable=True)
    effective_date = Column(Date, nullable=False)
    reason = Column(Text, nullable=True)
    origin = Column(Text, nullable=False, default="manual")
    created_at = Column(Text, nullable=False)


class AdvisorReview(Base):
    """复盘（三期-4 W2，ADR-0014 D6；迁移 0021）——快照/锚点不可变。

    `snapshot_json` 为 schema=1 稳定契约；`snapshot_sha256` 读时重算校验。
    `closed` 后结论/归因不可改；禁 DELETE。
    """
    __tablename__ = "advisor_reviews"
    __table_args__ = (
        CheckConstraint("status IN ('open','closed')", name="ck_advisor_review_status"),
        Index("ix_advisor_reviews_goal", "goal_id", "as_of"),
        Index("ix_advisor_reviews_versions", "goal_version_id", "start_goal_version_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    goal_id = Column(Integer, ForeignKey("goals.id"), nullable=False)
    goal_version_id = Column(Integer, ForeignKey("goal_versions.id"), nullable=False)
    start_goal_version_id = Column(Integer, ForeignKey("goal_versions.id"),
                                   nullable=True)
    as_of = Column(Date, nullable=False)
    start_as_of = Column(Date, nullable=True)
    label = Column(Text, nullable=True)
    note = Column(Text, nullable=True)
    status = Column(Text, nullable=False, default="open")
    snapshot_json = Column(Text, nullable=False)
    snapshot_sha256 = Column(String(64), nullable=False)
    attribution_json = Column(Text, nullable=True)
    conclusion = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)
    closed_at = Column(Text, nullable=True)


class AdvisorReviewSuggestion(Base):
    """复盘选定的已采纳建议批次（write-once，UNIQUE(review_id,suggestion_id)）。"""
    __tablename__ = "advisor_review_suggestions"
    __table_args__ = (
        UniqueConstraint("review_id", "suggestion_id",
                         name="uq_advisor_review_suggestion"),
        Index("ix_advisor_review_suggestions_suggestion", "suggestion_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(Integer, ForeignKey("advisor_reviews.id"), nullable=False)
    suggestion_id = Column(Integer, ForeignKey("advisor_suggestions.id"),
                           nullable=False)


class AdvisorReviewProposal(Base):
    """复盘结构化提案（ADR-0014 D7/D8）：proposed→adopted|dismissed。"""
    __tablename__ = "advisor_review_proposals"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('goal_version','threshold','allocation_target','narrative')",
            name="ck_advisor_review_proposal_kind"),
        CheckConstraint(
            "status IN ('proposed','adopted','dismissed')",
            name="ck_advisor_review_proposal_status"),
        Index("uq_advisor_review_proposal_content",
              "review_id", "kind", text("COALESCE(subject_ref,'')"),
              "payload_sha256", unique=True,
              sqlite_where=text("status = 'proposed'")),
        Index("ix_advisor_review_proposals_review", "review_id", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    review_id = Column(Integer, ForeignKey("advisor_reviews.id"), nullable=False)
    kind = Column(Text, nullable=False)
    subject_ref = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    rationale = Column(Text, nullable=False)
    status = Column(Text, nullable=False, default="proposed")
    decided_at = Column(Text, nullable=True)
    decision_note = Column(Text, nullable=True)
    applied_kind = Column(Text, nullable=True)
    applied_ref = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)


class AssetClass(Base):
    """资产类别树（ADR-0019 D1/D8）＝取代旧 `attributes(type='fund_type')`。

    一棵树覆盖资产与负债两侧，用 `side_scope` 声明该节点能挂哪一侧：
    `asset` / `liability` / `any`（仅分支节点出现，叶子恒为具体一侧）。
    `code` 为稳定标识（A1/B3/D2/H1…），`name` 可改而不影响引用。
    叶子性由 0026 的触发器守（有子节点者不得被引用）。
    """
    __tablename__ = "asset_classes"
    __table_args__ = (
        UniqueConstraint("code", name="uq_asset_classes_code"),
        UniqueConstraint("parent_id", "name", name="uq_asset_classes_parent_name"),
        Index("ix_asset_classes_parent", "parent_id"),
        CheckConstraint("side_scope IN ('asset','liability','any')",
                        name="ck_asset_classes_side_scope"),
        CheckConstraint("level >= 1", name="ck_asset_classes_level"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(10), nullable=False)
    name = Column(String(50), nullable=False)
    parent_id = Column(Integer, ForeignKey("asset_classes.id"), nullable=True)
    level = Column(Integer, nullable=False)
    side_scope = Column(String(10), nullable=False)

    parent = relationship("AssetClass", remote_side=[id], backref="children")


class Institution(Base):
    """金融机构/自持主体（ADR-0019 D3）＝取代旧 `attributes(type='account')` 的机构语义。

    `kind` 是机构类型，不是资产类别：`bank/broker/fund/insurer/gov/platform/other`。
    「自持」「个人」也在这张表里（`other`），因为两者都是资产项的第一段主语。
    """
    __tablename__ = "institutions"
    __table_args__ = (
        UniqueConstraint("name", name="uq_institutions_name"),
        CheckConstraint(
            "kind IN ('bank','broker','fund','insurer','gov','platform','other')",
            name="ck_institutions_kind"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    kind = Column(String(20), nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default=text("1"))

    # 截图来源组 N:M（0027：原 attributes(type='account') 关联改指机构）
    source_groups = relationship("SourceGroup", secondary="source_group_accounts",
                                 back_populates="institutions")


class Channel(Base):
    """触达渠道（ADR-0019 D3）＝机构的下级入口（App/网点/直销）。

    与机构拆开是因为「同一机构多渠道＝多条资产项」（拍板 7），渠道参与区分而不参与归类。
    当前存量全部可归到机构层，因此迁移后表为空；`asset_items.channel_id` 可空。
    """
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("name", name="uq_channels_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default=text("1"))


class Member(Base):
    """家庭成员/归属主体（ADR-0019 D6）＝取代旧 `attributes(type='owner')` 的真人姓名行。

    只存角色不存姓名：`self/spouse/joint/child/nominee`。旧库把家庭成员姓名直接写进
    属性名，既进不了版本库也无法迁移到别的设备，且「家庭池」与真实姓名混在同一命名空间。
    """
    __tablename__ = "members"
    __table_args__ = (
        UniqueConstraint("name", name="uq_members_name"),
        CheckConstraint("role IN ('self','spouse','joint','child','nominee')",
                        name="ck_members_role"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    role = Column(String(20), nullable=False)
    active = Column(Boolean, nullable=False, default=True, server_default=text("1"))


class Tag(Base):
    """标签字典（ADR-0019 D7）：受控标签（系统预置）+ 自由标签（设置页显式创建）。"""
    __tablename__ = "tags"
    __table_args__ = (
        UniqueConstraint("name", name="uq_tags_name"),
        CheckConstraint("kind IN ('controlled','free')", name="ck_tags_kind"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    kind = Column(String(20), nullable=False)


class AssetItemTag(Base):
    """资产项 × 标签（多值维度）。标签挂项不挂记录：一次标注覆盖全历史。"""
    __tablename__ = "asset_item_tags"
    __table_args__ = (
        Index("ix_asset_item_tags_tag", "tag_id"),
    )

    item_id = Column(Integer, ForeignKey("asset_items.id"), primary_key=True, nullable=False)
    tag_id = Column(Integer, ForeignKey("tags.id"), primary_key=True, nullable=False)


class AssetItemAttrHistory(Base):
    """资产项分类变更史（append-only，ADR-0019 D10）。

    分类从记录级上移到项级之后，「这条记录当时怎么分类的」不再可查，
    因此每次分类变更必须留一行：谁、什么时候、把哪个维度从什么改成什么。
    存储层由 0026 触发器拒绝 UPDATE/DELETE；更正＝追加新行。
    """
    __tablename__ = "asset_item_attr_history"
    __table_args__ = (
        Index("ix_item_attr_history_item", "item_id", "changed_at"),
        CheckConstraint(
            "dim IN ('asset_class','liquidity','institution','channel','owner','risk','tag')",
            name="ck_item_attr_history_dim"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    dim = Column(String(20), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    changed_at = Column(DateTime, nullable=False, default=datetime.now)
    changed_by = Column(String(50), nullable=True)
    reason = Column(Text, nullable=True)


class AssetItem(Base):
    """资产项（聚合根，v5 身份层，系统架构 §2）。

    身份＝持久代理 ID（拍板 7：同标的多渠道＝分渠道多条资产项）；
    display_name 与身份解耦（拍板 10：改名不改身份）；channel 为机构渠道
    信息列（不参与唯一性，唯一锚＝legacy_name，避免渠道误判分裂身份）；
    status 承载 R-37 状态机（active→archived，归档是人裁决动作）。
    legacy_name＝V4 全名回填锚（UNIQUE；迁移后新建项为 NULL）。

    分类七维（ADR-0019 D2）：从记录级上移到本表。`asset_class_id` / `liquidity_days` /
    `institution_id` / `owner_id` 四项必填（`status='draft'` 期间豁免），`channel_id` /
    `risk_level` 可空，标签走 `asset_item_tags`。基数**由 schema 保证**，不靠应用层规矩——
    这是本次重构的全部要点：旧模型把它们塞进 `attributes(type)` 单表，同维多值 75 处、
    记录级打点 15,892 行仍无人守得住。

    `owner_id` 同时是**归属的唯一真相源**（`asset_item_owner_changes` 只是审计史）；
    写入单点是 `services.asset_owner_service.set_item_owner`，它保证列与史同事务。
    """
    __tablename__ = "asset_items"
    __table_args__ = (
        Index("ix_asset_items_status", "status"),
        Index("ix_asset_items_class", "asset_class_id"),
        Index("ix_asset_items_institution", "institution_id"),
        Index("ix_asset_items_owner", "owner_id"),
        CheckConstraint("status IN ('active','archived','draft')",
                        name="ck_asset_items_status"),
        # draft 是「分类未完」的显式状态：只有它允许四维为空，转 active 即强制补齐。
        # archived 一并豁免：归档是终态，不再参与录入校验；且存量归档项在 0026 迁移中
        # 拿不到分类，若强制补齐会让迁移无法落地（新库导入本来就会带上分类）。
        CheckConstraint(
            "status IN ('draft','archived') OR (asset_class_id IS NOT NULL"
            " AND liquidity_days IS NOT NULL"
            " AND institution_id IS NOT NULL"
            " AND owner_id IS NOT NULL)",
            name="ck_asset_items_dims_required"),
        CheckConstraint(
            "liquidity_days IS NULL OR liquidity_days IN (0,1,7,30,90,180,365,9999)",
            name="ck_asset_items_liquidity_days"),
        CheckConstraint(
            "risk_level IS NULL OR risk_level IN ('R1','R2','R3','R4','R5')",
            name="ck_asset_items_risk_level"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    display_name = Column(String(100), nullable=False)
    channel = Column(String(100), nullable=True)
    status = Column(String(10), nullable=False, default="active",
                    server_default=text("'active'"))  # active | archived | draft
    legacy_name = Column(String(100), nullable=True, unique=True)
    # 归档生效日（0025，ADR-0017 D2 修订）：归档＝自该日起终止计量，历史值保留。
    # archived 且 NULL＝归档日未知 → 保守地对所有 as-of 日期剔除；解除归档清回 NULL。
    archived_effective_from = Column(Date, nullable=True)
    # 分类七维（ADR-0019）：单值维度用列，基数由 NOT NULL/CHECK 兜底。
    asset_class_id = Column(Integer, ForeignKey("asset_classes.id"), nullable=True)
    liquidity_days = Column(Integer, nullable=True)   # 变现所需天数，白名单见 CHECK
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=True)
    owner_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=True)
    risk_level = Column(String(2), nullable=True)     # R1..R5，可空
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    # 项级备注（0039，ADR-0031 D0）：单条可编辑、允许多行，上限 500 字符由应用层
    # 校验（trim 后计，trim 后空串写 NULL）。挂项不挂记录——记录是 write-once 快照，
    # 元数据挂记录会在每次更正时丢一次。**列序必须排在末位**：迁移走
    # `ALTER TABLE ADD COLUMN`（物理末列），与 create_all 的列序保持一致。
    note = Column(Text, nullable=True)

    asset_class = relationship("AssetClass")
    institution = relationship("Institution")
    owner = relationship("Member")
    tags = relationship("Tag", secondary="asset_item_tags", lazy="selectin")


class AssetItemName(Base):
    """显示名史 / 别名（append-only，拍板 10；ADR-0026 扩 reason/effect）。

    三类行：
    - `origin='migration'`：建项时首个名字；
    - `origin='rename'`：改名事件追加的一行（旧名补登与新名各一行），
      `effective_from`＝生效日，`reason`/`effect` 记录这次改名的原因与回写范围；
    - `origin='alias'`：录入关联（bind）沉淀的别名，**不设生效日**，
      用于「同一持仓换了识别名」的后续自动命中；不可撤销（只加不删）。
    历史名与别名两轴正交：历史名有区间、别名没有。
    """
    __tablename__ = "asset_item_names"
    __table_args__ = (
        Index("ix_asset_item_names_item", "item_id"),
        Index("ix_asset_item_names_name", "name"),
        CheckConstraint("origin IN ('migration','rename','alias')",
                        name="ck_asset_item_names_origin"),
        CheckConstraint(
            "reason IS NULL OR reason IN ('correction','evolution','normalization')",
            name="ck_asset_item_names_reason"),
        CheckConstraint(
            "effect IS NULL OR effect IN ('current','history_backfill')",
            name="ck_asset_item_names_effect"),
        CheckConstraint(
            "(origin = 'alias' AND effective_from IS NULL)"
            " OR (origin <> 'alias' AND effective_from IS NOT NULL)",
            name="ck_asset_item_names_alias_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    name = Column(String(100), nullable=False)
    effective_from = Column(Date, nullable=True)   # alias 行为 NULL
    origin = Column(String(10), nullable=False)    # migration | rename | alias
    reason = Column(String(20), nullable=True)     # correction|evolution|normalization
    effect = Column(String(20), nullable=True)     # current|history_backfill
    reason_note = Column(String(200), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AssetItemRenameToken(Base):
    """改名确认令牌（ADR-0026 D11：预览 → 确认两步，一次性）。

    预览端点算影响面并落一行；确认端点校验 token 未用、未过期、载荷与影响面哈希
    都一致后才执行。一次性＝`used_at` 非空即作废（行永不删，留痕）。
    """
    __tablename__ = "asset_item_rename_tokens"
    __table_args__ = (
        Index("ix_asset_item_rename_tokens_item", "item_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), nullable=False, unique=True)
    item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    payload_json = Column(Text, nullable=False)
    impact_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class AssetItemOwnerChange(Base):
    """资产项归属**变更史**（append-only，ADR-0011；ADR-0019 D6 换值域）。

    归属的唯一真相源是 `asset_items.owner_id`（值域 `members.id`）——本表**不再**
    承载当前值，只回答「归属怎么变成现在这样」。历史上它曾被当作现值读，而写路径
    只追加本表不改列，导致「改了归属，列表/导出看不到」，故收口到列（见
    `services/asset_owner_service`）。

    - 写入必须与 `asset_items.owner_id` 同事务（`set_item_owner` 单点）；
    - 家庭池＝`owner_kind='family_pool'` + `owner_id=NULL` 属**存量形状**：
      该合成归属随 D6 退役，新写入一律 `member` + `members.id`；
    - 存储层由 0016 触发器拒绝 UPDATE/DELETE；更正＝追加新行（无 update/delete 端口）；
    - `origin`：migration（0016/0022 回填）| manual（人裁决/旧入口翻译）；
    - 同日可多次变更（更正＝追加）；仅回填行受 `uq_item_owner_migration`
      （`UNIQUE(item, effective_date) WHERE origin='migration'`）约束。
    """
    __tablename__ = "asset_item_owner_changes"
    __table_args__ = (
        CheckConstraint("owner_kind IN ('member', 'family_pool')",
                        name="ck_item_owner_kind"),
        CheckConstraint("origin IN ('manual', 'migration')",
                        name="ck_item_owner_origin"),
        CheckConstraint(
            "(owner_kind='member' AND owner_id IS NOT NULL)"
            " OR (owner_kind='family_pool' AND owner_id IS NULL)",
            name="ck_item_owner_pair"),
        Index("uq_item_owner_migration", "asset_item_id", "effective_date",
              unique=True, sqlite_where=text("origin = 'migration'")),
        Index("ix_item_owner_item_eff", "asset_item_id", "effective_date", "id"),
        Index("ix_item_owner_owner", "owner_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    owner_kind = Column(Text, nullable=False)             # member | family_pool
    # ADR-0019 D6：归属主体改指 members（旧库指向 attributes.id 的真人姓名行）
    owner_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    effective_date = Column(Date, nullable=False)
    reason = Column(Text, nullable=True)
    origin = Column(Text, nullable=False, default="manual",
                    server_default=text("'manual'"))
    created_at = Column(Text, nullable=False)             # ISO 字符串


class PrincipalChange(Base):
    """本金口径变更史（v5 二期·2.1）＝权威真源，**append-only 映射**。

    - new_amount 带符号（注入正/撤出负，可为负＝净撤出）；effective_date 生效日
      （R-36：历史期按当时生效口径定格）；reason 必填（R-9 人裁决标注）。
    - 存储层由 0012 触发器拒绝 UPDATE/DELETE；更正＝追加新行（resolve 按
      effective_date DESC, id DESC 决胜）；无应用层修改端口。
    - origin（external|transfer|lineage）与 transfer_event_id 为 2.3/2.4 预留列，
      本步恒 'external'/NULL。当前生效口径＝resolve 结果，不设独立现值列（双真源禁忌）。
    """
    __tablename__ = "principal_changes"
    __table_args__ = (
        Index("ix_principal_item_eff", "asset_item_id", "effective_date", "id"),
        Index("ix_principal_evidence", "evidence_ref"),
        CheckConstraint("origin IN ('external','transfer','lineage')",
                        name="ck_principal_origin"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    new_amount = Column(Numeric(15, 2), nullable=False)
    effective_date = Column(Date, nullable=False)
    reason = Column(Text, nullable=False)
    evidence_ref = Column(Integer, ForeignKey("screenshot_evidence.id"), nullable=True)
    origin = Column(Text, nullable=False, default="external",
                    server_default=text("'external'"))
    transfer_event_id = Column(Integer, nullable=True)   # 2.3 预留
    created_at = Column(Text, nullable=False)            # ISO 字符串


class TransferEvent(Base):
    """内部买卖转移事件（v5 二期·2.3，spec §2.3）＝登记凭证行，**append-only 映射**。

    - amount **恒正**（方向由 from/to 表达，CHECK 锁定）；event_date 登记日＝快照日
      （R-34 注：两侧当日均须有 active 快照行，缺失 → SNAPSHOT_REQUIRED 阻断）。
    - 登记在**同一事务**生成两条 principal_changes（from −amount / to +amount，
      origin='transfer'、transfer_event_id 回填）——两侧本金等额、净值不变、
      不产生流水；更正＝登记**反向转移**（追加，不改史，无 update/delete 端口）。
    - 存储层由 0013 触发器拒绝 UPDATE/DELETE；审计 action＝transfer_register。
    """
    __tablename__ = "transfer_events"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_transfer_amount_positive"),
        Index("ix_transfer_from", "from_item_id", "event_date"),
        Index("ix_transfer_to", "to_item_id", "event_date"),
        Index("ix_transfer_evidence", "evidence_ref"),
        CheckConstraint("from_item_id <> to_item_id", name="ck_transfer_distinct"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    to_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    event_date = Column(Date, nullable=False)
    reason = Column(Text, nullable=False)
    evidence_ref = Column(Integer, ForeignKey("screenshot_evidence.id"), nullable=True)
    created_at = Column(Text, nullable=False)            # ISO 字符串


class LineageLink(Base):
    """血缘链接（v5 二期·2.4，spec §2.4）＝前身/后继链，**append-only、创建即固定**（拍板 8）。

    - from_item_id＝旧项（登记即归档），to_item_id＝新项（口径承接落于此项）。
    - link_type ∈ merge|split|revision；**改名不走血缘**（显示名史已承载，拍板 10，
      无 rename 类型）；split_ratio 仅 split 携带（(0,1] 由服务层校验）。
    - 存储层由 0014 触发器拒绝 UPDATE/DELETE；无应用层修改端口。
    - 口径承接＝同事务新项初始 `principal_changes`（origin='lineage'，0012 预留值；
      transfer_event_id 恒 NULL 为 2.3 专用），值＝人确认（预填可改）。
    """
    __tablename__ = "lineage_links"
    __table_args__ = (
        CheckConstraint("link_type IN ('merge', 'split', 'revision')",
                        name="ck_lineage_type"),
        Index("ix_lineage_from", "from_item_id", "id"),
        Index("ix_lineage_to", "to_item_id", "id"),
        CheckConstraint("from_item_id <> to_item_id", name="ck_lineage_distinct"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    to_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    link_type = Column(Text, nullable=False)             # merge | split | revision
    split_ratio = Column(Numeric(6, 4), nullable=True)   # 仅 split
    note = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)            # ISO 字符串


class ItemComposition(Base):
    """父子**构成**边（ADR-0029 D3，迁移 0037）——「父项由哪些子项构成」。

    与血缘（`lineage_links`）语义**不相容**，故独立成表：血缘是「接替」，写入即归档
    前身；构成是「父子同时在世」，父项只是退出**计量**（净值/占比/告警/导出合计），
    展示口径照常列出并标注「汇总项 · 不计入合计」（ADR-0029 D1/D14）。

    - 一子一父：`uq_item_compositions_child`（撞约束 → 409 `COMPOSITION_PARENT_CONFLICT`）；
      换父必须先解除旧关系，不做隐式接管（票 02 D2）。
    - 允许多层、禁环：DB 只兜自环（`ck_item_compositions_not_self`），祖先链检测在
      服务层（`composition_service.create_edge` → 409 `COMPOSITION_CYCLE`）。
    - **无权重列**（D3）：全额归属，不做比例分摊。
    - 判定是**派生**的：某项只要作为 `parent_item_id` 出现即退出计量，不存布尔位——
      存标记则解除关系时忘清一次就永久漏计，且界面上看不出异常（D1）。

    计量谓词单点在 `services.snapshot_identity.active_item_predicate`。
    """
    __tablename__ = "item_compositions"
    __table_args__ = (
        UniqueConstraint("child_item_id", name="uq_item_compositions_child"),
        # 父侧 EXISTS：计量谓词对每个 as_of 查询都要跑，必须有索引
        Index("ix_item_compositions_parent", "parent_item_id"),
        CheckConstraint("parent_item_id <> child_item_id",
                        name="ck_item_compositions_not_self"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    child_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    note = Column(String(200), nullable=True)   # 为什么这样归（≤200，应用层校验）
    created_at = Column(DateTime, nullable=False, default=datetime.now)


class Goal(Base):
    """C1 目标聚合根（ADR-0010 §3.1，迁移 0015）。

    scope 在根不在版本内（改范围＝换目标）；`scope_kind='member'` 列本期只留、
    API 422（四期启用）。`is_primary` 全表至多一条 1（部分唯一索引）；只有带
    active 版本的 Goal 可为 1（draft-only 恒 0，服务层保证）。
    """
    __tablename__ = "goals"
    __table_args__ = (
        CheckConstraint("scope_kind IN ('family', 'member')", name="ck_goals_scope"),
        CheckConstraint(
            "(scope_kind='family' AND scope_owner_id IS NULL)"
            " OR (scope_kind='member' AND scope_owner_id IS NOT NULL)",
            name="ck_goals_scope_owner"),
        CheckConstraint("is_primary IN (0, 1)", name="ck_goals_primary"),
        Index("uq_goals_primary", "is_primary", unique=True,
              sqlite_where=text("is_primary = 1")),
        Index("ix_goals_scope_owner", "scope_owner_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(Text, nullable=False)
    scope_kind = Column(Text, nullable=False, default="family")
    scope_owner_id = Column(Integer, ForeignKey("members.id"), nullable=True)
    is_primary = Column(Integer, nullable=False, default=0)
    created_at = Column(Text, nullable=False)            # ISO 字符串


class GoalVersion(Base):
    """目标版本（追加史，ADR-0010 §3.2，迁移 0015）。

    每次变更＝新版本＋理由；`version_no` 每 Goal 从 1 单调递增；
    `active` 每 Goal 至多一条（部分唯一索引兜底并发）。active/archived 内容
    不可改（service + 测试保证，无存储层触发器：draft 需改/删）。
    `criterion_json` ＝激活时的判据/分区定格（本期 kind=null，读端不求值）。
    """
    __tablename__ = "goal_versions"
    __table_args__ = (
        UniqueConstraint("goal_id", "version_no", name="uq_goal_versions_no"),
        CheckConstraint("version_no > 0", name="ck_goal_versions_no"),
        CheckConstraint("status IN ('draft', 'active', 'archived')",
                        name="ck_goal_versions_status"),
        CheckConstraint("target_amount > 0", name="ck_goal_versions_amount"),
        CheckConstraint("target_rate_annual IS NULL OR target_rate_annual > -1",
                        name="ck_goal_versions_rate"),
        CheckConstraint("risk_profile IS NULL OR risk_profile IN"
                        " ('conservative', 'balanced', 'growth')",
                        name="ck_goal_versions_risk"),
        CheckConstraint("life_stage IS NULL OR life_stage IN"
                        " ('accumulation', 'transition', 'decumulation')",
                        name="ck_goal_versions_life"),
        CheckConstraint("status <> 'active' OR effective_from IS NOT NULL",
                        name="ck_goal_versions_active_eff"),
        CheckConstraint("status <> 'active' OR criterion_json IS NOT NULL",
                        name="ck_goal_versions_active_criterion"),
        CheckConstraint("effective_from IS NULL OR target_date > effective_from",
                        name="ck_goal_versions_date_order"),
        Index("uq_goal_versions_active", "goal_id", unique=True,
              sqlite_where=text("status = 'active'")),
        Index("ix_goal_versions_goal", "goal_id", "version_no"),
        Index("ix_goal_versions_status", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    goal_id = Column(Integer, ForeignKey("goals.id"), nullable=False)
    version_no = Column(Integer, nullable=False)
    status = Column(Text, nullable=False, default="draft")
    target_amount = Column(Numeric(15, 2), nullable=False)
    target_date = Column(Date, nullable=False)
    target_rate_annual = Column(Numeric(8, 4), nullable=True)
    risk_profile = Column(Text, nullable=True)
    life_stage = Column(Text, nullable=True)
    reason = Column(Text, nullable=False)
    effective_from = Column(Date, nullable=True)
    criterion_json = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)
    activated_at = Column(Text, nullable=True)
    archived_at = Column(Text, nullable=True)


class AdvisorInfoSet(Base):
    """建议信息集头（三期-3 W2，ADR-0013 D4；迁移 0019）——write-once。

    仅对参考区 `goals/goal_versions` 建 FK；账本实体一律以文本 `source_ref` 引用。
    """
    __tablename__ = "advisor_info_sets"
    __table_args__ = (
        Index("ix_advisor_info_sets_goal", "goal_id", "goal_version_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(Text, nullable=False)
    as_of = Column(Date, nullable=True)
    goal_id = Column(Integer, ForeignKey("goals.id"), nullable=True)
    goal_version_id = Column(Integer, ForeignKey("goal_versions.id"), nullable=True)
    builder_version = Column(Text, nullable=False)
    entry_count = Column(Integer, nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    meta_json = Column(Text, nullable=True)


class AdvisorInfoEntry(Base):
    """信息集条目（write-once，`UNIQUE(info_set_id,key)`）——封闭键空间。"""
    __tablename__ = "advisor_info_entries"
    __table_args__ = (
        UniqueConstraint("info_set_id", "key", name="uq_advisor_entry_key"),
        Index("ix_advisor_entries_set", "info_set_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    info_set_id = Column(Integer, ForeignKey("advisor_info_sets.id"),
                         nullable=False)
    key = Column(Text, nullable=False)
    kind = Column(Text, nullable=False)             # money/ratio/count/date/state/text
    label = Column(Text, nullable=False)            # 通用类目名（不含账户/机构/持仓名）
    value = Column(Text, nullable=False)
    value_scaled = Column(Text, nullable=True)      # 仅 money：万元/1 位
    unit = Column(Text, nullable=False)             # CNY/ratio/pp/days/state
    trust = Column(Text, nullable=False)            # measured/carried/partial/unavailable
    source = Column(Text, nullable=False)           # ledger/principal/goal/allocation/signal/metric
    source_ref = Column(Text, nullable=True)        # TEXT 引用，无 FK
    as_of = Column(Date, nullable=True)


class AdvisorSuggestion(Base):
    """建议单：内容列生成期外 write-once；状态/时间戳列可改（0019 触发器）。"""
    __tablename__ = "advisor_suggestions"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_advisor_dedupe_key"),
        CheckConstraint(
            "status IN ('generating','awaiting_review','accepted','rejected',"
            "'expired','gate_failed','generation_failed')",
            name="ck_advisor_suggestion_status"),
        Index("ix_advisor_suggestions_status", "status"),
        Index("ix_advisor_suggestions_infoset", "info_set_id"),
        Index("ix_advisor_suggestions_goal", "goal_id", "goal_version_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    info_set_id = Column(Integer, ForeignKey("advisor_info_sets.id"), nullable=True)
    goal_id = Column(Integer, ForeignKey("goals.id"), nullable=True)
    goal_version_id = Column(Integer, ForeignKey("goal_versions.id"), nullable=True)
    trigger_kind = Column(Text, nullable=False)     # manual/signal/sched
    trigger_ref = Column(Text, nullable=True)
    dedupe_key = Column(Text, nullable=False)
    horizon = Column(Text, nullable=True)           # short/mid/long
    status = Column(Text, nullable=False, default="generating")
    title = Column(Text, nullable=True)
    summary_template = Column(Text, nullable=True)
    summary_rendered = Column(Text, nullable=True)
    gate_json = Column(Text, nullable=True)
    gate_version = Column(Text, nullable=True)
    prompt_version = Column(Text, nullable=True)
    provider_id = Column(Integer, nullable=True)    # 快照，无 FK
    provider_label = Column(Text, nullable=True)
    model = Column(Text, nullable=True)
    expires_at = Column(Date, nullable=True)
    created_at = Column(Text, nullable=False)
    generated_at = Column(Text, nullable=True)
    closed_at = Column(Text, nullable=True)
    request_id = Column(Text, nullable=True)


class AdvisorSuggestionItem(Base):
    """建议项（write-once，`UNIQUE(suggestion_id,seq)`）。"""
    __tablename__ = "advisor_suggestion_items"
    __table_args__ = (
        UniqueConstraint("suggestion_id", "seq", name="uq_advisor_item_seq"),
        CheckConstraint("subject_kind IN ('attribute','goal','threshold','narrative')",
                        name="ck_advisor_item_subject"),
        CheckConstraint("direction IN ('increase','decrease','hold','rebalance')",
                        name="ck_advisor_item_direction"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    suggestion_id = Column(Integer, ForeignKey("advisor_suggestions.id"),
                           nullable=False)
    seq = Column(Integer, nullable=False)
    subject_kind = Column(Text, nullable=False)
    subject_ref = Column(Text, nullable=True)
    direction = Column(Text, nullable=False)
    rationale_template = Column(Text, nullable=False)
    rationale_rendered = Column(Text, nullable=False)
    info_refs_json = Column(Text, nullable=False)


class AdvisorDecision(Base):
    """审阅动作（append-only；终态动作部分唯一）。"""
    __tablename__ = "advisor_decisions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('accept','reject','expire','execution_note')",
            name="ck_advisor_decision_action"),
        CheckConstraint("actor IN ('owner','system')", name="ck_advisor_decision_actor"),
        CheckConstraint(
            "execution_status IS NULL OR execution_status IN"
            " ('done','partial','not_done')",
            name="ck_advisor_decision_exec"),
        Index("uq_advisor_terminal_decision", "suggestion_id", unique=True,
              sqlite_where=text("action IN ('accept','reject','expire')")),
        Index("ix_advisor_decisions_suggestion", "suggestion_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    suggestion_id = Column(Integer, ForeignKey("advisor_suggestions.id"),
                           nullable=False)
    action = Column(Text, nullable=False)
    reason = Column(Text, nullable=True)
    execution_status = Column(Text, nullable=True)
    decided_at = Column(Text, nullable=False)
    actor = Column(Text, nullable=False)
    request_id = Column(Text, nullable=True)


class AdvisorJob(Base):
    """建议生成任务（会话/任务区，可弃；>7 天终态可清理）。"""
    __tablename__ = "advisor_jobs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','done','error')",
                        name="ck_advisor_job_status"),
        CheckConstraint("stage IN ('queued','info_set','llm','gate','done')",
                        name="ck_advisor_job_stage"),
        Index("ix_advisor_jobs_status", "status"),
    )

    job_id = Column(Text, primary_key=True)
    suggestion_id = Column(Integer, nullable=True)
    status = Column(Text, nullable=False, default="queued")
    stage = Column(Text, nullable=False, default="queued")
    attempts = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)
    created_at = Column(Text, nullable=False)
    started_at = Column(Text, nullable=True)
    finished_at = Column(Text, nullable=True)


class AdvisorSettings(Base):
    """建议设置（单行 id=1；自动触发默认关闭，冷却 7 天）。"""
    __tablename__ = "advisor_settings"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_advisor_settings_single"),
        CheckConstraint("auto_enabled IN (0,1)", name="ck_advisor_settings_auto"),
    )

    id = Column(Integer, primary_key=True)
    auto_enabled = Column(Integer, nullable=False, default=0)
    cooldown_days = Column(Integer, nullable=False, default=7)
    default_horizon = Column(Text, nullable=False, default="mid")
    last_auto_at = Column(Text, nullable=True)
    updated_at = Column(Text, nullable=False)


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (
        Index("ix_assets_date", "asset_date"),
        Index("ix_assets_item", "asset_item_id"),
        # v5 身份层（业务架构 R-4 落位）：同日同项唯一有效行；历史（superseded）
        # 行只追加不删。旧 uq(asset_date, asset_name) 由 0008 重建删除。
        Index("uq_assets_item_date_active", "asset_item_id", "asset_date", unique=True,
              sqlite_where=text("superseded_by IS NULL")),
        Index("ix_assets_superseded", "superseded_by"),
        Index("ix_assets_evidence", "source_evidence_id"),
        CheckConstraint("amount >= 0", name="ck_assets_amount_non_negative"),
        CheckConstraint("side IN ('asset','liability')", name="ck_assets_side"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    asset_name = Column(String(100), nullable=False)  # 写入时显示名快照（时点语义，改名不回写）
    amount = Column(Numeric(15, 2), nullable=False)
    side = Column(String(10), nullable=False, default="asset")  # asset / liability
    asset_date = Column(Date, nullable=False)
    source_evidence_id = Column(Integer, ForeignKey("screenshot_evidence.id"), nullable=True)
    # v5 身份层：资产项 FK。ORM 层可空（双轨窗口 + 测试兼容）；迁移 0008 重建后
    # 物理表为 NOT NULL，运行期写路径必须经 ledger 写函数（get_or_create_item 单点）。
    asset_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=True)
    # 更正＝追加的旧行指针（write-once 金额本体；净值/占比取 IS NULL 的 active 行）
    superseded_by = Column(Integer, ForeignKey("assets.id"), nullable=True)
    # v5 二期·2.1：建行时冗余「当日生效口径」（NULL＝存量/未回填，读端回退变更史）。
    # 真源是 principal_changes，此列仅解析加速；不回填历史行。
    cost_basis = Column(Numeric(15, 2), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    # 只读视图：快照行 → 资产项（display_name 取数用；写入永远经 ledger 单点）
    item = relationship("AssetItem", viewonly=True)


class ScreenshotEvidence(Base):
    """截图凭证：截图原图 + SHA256 + 录入时间 + 来源组。

    审计血缘：资产记录 → ScreenshotEvidence → data/evidence/<年>/<月>/ 原图文件。
    凭证永久保留，无删除/清理入口（见 spec 非目标）。
    """
    __tablename__ = "screenshot_evidence"
    __table_args__ = (
        Index("ix_screenshot_evidence_group_date", "source_group_id", "data_date"),
        # 同一张图在同一来源组只存一份；跨组同图各存一份（ADR-0024 D2）。
        # 必须是复合索引：单列 sha 唯一会拒绝「一图属两组」，与同组查重（D1）冲突。
        Index("ux_screenshot_evidence_sha_group", "file_sha256", "source_group_id",
              unique=True),
        CheckConstraint("engine IS NULL OR engine IN ('ocr','ai')",
                        name="ck_evidence_engine"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 来源组：外键指向 source_groups.id（0029 起）。此前是 VARCHAR slug 快照，
    # 组改名要靠应用层级联同步，漏一次就留悬空引用——组身份是 id，不是名字。
    source_group_id = Column(Integer, ForeignKey("source_groups.id"), nullable=False)
    file_path = Column(String(500), nullable=False)    # data/evidence 相对路径
    file_sha256 = Column(String(64), nullable=False)
    original_filename = Column(String(255), nullable=True)
    data_date = Column(Date, nullable=True)            # 截图内数据日期（或无则写入日）
    supplement_rows = Column(Text, nullable=True)  # 补录审计 JSON 文本（无补录 → NULL）
    # 识别引擎（骨架来源）审计列："ocr" | "ai"；历史行 NULL，语义 = "ocr"
    engine = Column(String(10), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    # 原图信息（JSON 白名单：width/height/bytes/format + exif 子集，不存 GPS）。
    # 只写不改：一 sha 一行一文件，重复上传复用既有行（ADR-0022 D2）。读不出 → NULL。
    image_meta = Column(Text, nullable=True)
    # 识别快照（JSON，schema:1，`recognized` 与 `adopted` 两段对照），供日后识别率检查。
    # 写入只经 evidence_service 的单一函数，不在路由里各写一遍（ADR-0023 D7）。
    recognition_json = Column(Text, nullable=True)


class ScreenshotBatch(Base):
    """截图录入批次（ADR-0029 D5/d8，迁移 0037；票 04 D1）。

    批次是**真实业务对象**，不是前端 state：多页截图里一张图可能只有明细、另一张图
    只有汇总行，跨图核对必须有一个可留痕的载体。生命周期四态：

    - `open`：已建壳，一个成员都还没登记（前端逐图 apply，成员随后陆续补齐）；
    - `partial`：**已有成员、但还没 finalize**（允许部分成功，**永不跨图回滚**，票 04 D5；
      「成员全到齐」也是 partial——`partial` 的语义是「未定案」，不是「有失败」，有失败时
      `screenshot_batch_members.status='failed'` 单独记账）；
    - `finalized`：`POST /api/screenshot-batches/{id}/finalize` 通过（唯一校验点，D4）；
    - `abandoned`：弃置（本期只允许置弃置，不做删除，见 spec §4）。

    推进规则**单向**（`open → partial → finalized`，`abandoned` 是旁路终态）：
    apply 成功登记成员即把 `open` 推进到 `partial`；finalize 唯一把批次置 `finalized`。
    没有回退路径：成员写失败只会让批次停在 `partial`，重试由前端逐图发起（D5）。

    `expected_members` 是**建批次时的预计成员数**（NULL＝未声明）：finalize 用它判
    「成员齐没齐」（409 `BATCH_NOT_COMPLETE`）。未声明时无法判断「齐」，只要求至少
    一条成员且全部 `applied`。

    不带 `batch_id` 的 apply 行为与本期一致（additive：批次是可选对象）。
    """
    __tablename__ = "screenshot_batches"
    __table_args__ = (
        Index("ix_screenshot_batches_status", "status"),
        CheckConstraint("status IN ('open','partial','finalized','abandoned')",
                        name="ck_screenshot_batches_status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    data_date = Column(Date, nullable=True)       # 截图内数据日期（跨日批次可空）
    source_group_id = Column(Integer, ForeignKey("source_groups.id"), nullable=True)
    status = Column(String(12), nullable=False, default="open",
                    server_default=text("'open'"))
    finalized_at = Column(DateTime, nullable=True)
    note = Column(String(200), nullable=True)
    # 预计成员数（票 04 D2「预计成员数」）；NULL＝未声明 → finalize 只要求非空且全 applied
    expected_members = Column(Integer, nullable=True)


class ScreenshotBatchMember(Base):
    """批次成员（一张图一行；票 04 D1/D2）。

    apply 成功才写一行（`applied_at` ＋ `status='applied'`）；`UNIQUE(batch_id,
    evidence_id)` 让「同一张图重复 apply」天然幂等（重复登记＝原地刷新，不新增行）。
    `status` 取值 `pending` / `applied` / `failed`：本期只写 `applied`（行只在写入
    成功时落），`pending` / `failed` 为「批次重试编排」预留（spec §4 明确不做，但
    取值域先留全，免得将来改 CHECK 又要重建表）。
    finalize 要求成员全部 `applied`，否则 409 `BATCH_NOT_COMPLETE`（details 列出
    已知成员 evidence_id 与缺失条数）。
    """
    __tablename__ = "screenshot_batch_members"
    __table_args__ = (
        UniqueConstraint("batch_id", "evidence_id",
                         name="uq_screenshot_batch_members_batch_evidence"),
        Index("ix_screenshot_batch_members_evidence", "evidence_id"),
        CheckConstraint("status IN ('pending','applied','failed')",
                        name="ck_screenshot_batch_members_status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("screenshot_batches.id"), nullable=False)
    evidence_id = Column(Integer, ForeignKey("screenshot_evidence.id"), nullable=False)
    applied_at = Column(DateTime, nullable=False, default=datetime.now)
    status = Column(String(12), nullable=False, default="applied",
                    server_default=text("'applied'"))


class ScreenshotReconciliation(Base):
    """一次跨图核对（汇总行 ← 若干明细行）的**本次**差额痕迹（票 04 D1/D3）。

    与 `item_compositions` 分工：构成表答「父子长久是谁」，本表答「这一次差多少、
    谁接受了」。差额**零容差**（D10）：只有改子项、补子项、显式接受三个出口；
    显式接受必须写 `accepted_reason`（DB CHECK 兜底，应用层先拦 400
    `RECONCILE_REASON_REQUIRED`），不接受「静默吸收差额」。

    金额三列一律 `NUMERIC(15,2)`（与 `assets.amount` 同精度），参与运算前由服务层转
    `Decimal`——本仓禁止浮点。
    """
    __tablename__ = "screenshot_reconciliations"
    __table_args__ = (
        Index("ix_screenshot_reconciliations_batch", "batch_id"),
        CheckConstraint("status IN ('matched','accepted_mismatch')",
                        name="ck_screenshot_reconciliations_status"),
        # 显式接受差额必须有人负责：无理由不得落成 accepted_mismatch
        CheckConstraint(
            "status <> 'accepted_mismatch' OR accepted_reason IS NOT NULL",
            name="ck_screenshot_reconciliations_reason"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    batch_id = Column(Integer, ForeignKey("screenshot_batches.id"), nullable=False)
    parent_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=False)
    # 可空：前端可能只给 item_id（冻结契约第 2 条），凭证 id 只用于追溯展示
    parent_evidence_id = Column(Integer, ForeignKey("screenshot_evidence.id"),
                                nullable=True)
    # 汇总行在 preview 里的行号；**可空**——父项也可能是用户手选的既有资产项，
    # 那个项在本批截图里没有行，`(evidence_id, row_index)` 无处可取（前端实测契约补丁）。
    # 判定「人工指定 vs 预览行」的唯一依据是 `parent_evidence_id is None`
    # （审计里记成 `parent_source: manual | preview_row`），本列只用于展示定位。
    parent_row_index = Column(Integer, nullable=True)
    aggregate_amount = Column(Numeric(15, 2), nullable=False)   # 汇总行金额
    child_amount_sum = Column(Numeric(15, 2), nullable=False)   # 子项合计
    diff = Column(Numeric(15, 2), nullable=False)               # aggregate − children
    status = Column(String(20), nullable=False)   # matched | accepted_mismatch
    accepted_reason = Column(String(200), nullable=True)
    accepted_at = Column(DateTime, nullable=True)


class ScreenshotReconciliationChild(Base):
    """核对记录的子项明细（一行＝一个子项/一张明细行；票 04 D1）。

    明细必须可追溯：`child_evidence_id` ＋ `child_row_index` 指回原始截图行，
    `child_item_id` 指回落库的资产项——核对记录本身不参与计量，只作留痕。

    三个 id/行号列**都可空**（冻结契约第 2 条）：子项 `item_id` 允许为 null，前端也
    可能只给金额；解析不到资产项时仍要留下「这一笔金额来自哪一行」，否则下次没人
    能复现这次核对。`amount` 恒非空（核对的最小事实）。
    """
    __tablename__ = "screenshot_reconciliation_children"
    __table_args__ = (
        Index("ix_screenshot_reconciliation_children_rec", "reconciliation_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    reconciliation_id = Column(Integer,
                               ForeignKey("screenshot_reconciliations.id"),
                               nullable=False)
    child_item_id = Column(Integer, ForeignKey("asset_items.id"), nullable=True)
    child_evidence_id = Column(Integer, ForeignKey("screenshot_evidence.id"),
                               nullable=True)
    child_row_index = Column(Integer, nullable=True)
    amount = Column(Numeric(15, 2), nullable=False)


class SourceGroup(Base):
    """截图来源组（一个 App 一个组），可管理 CRUD。

    与**机构**（institutions）为 N:M：一个来源组截图可覆盖多个机构账户，一个机构
    的资产可来自多个来源组截图（见 ADR-0003）。slug 是稳定标识符（如旧硬编码键
    'bantuou'），用于截图凭证 `source_group_id` 外键与前端回传（0029 起凭证存 id，
    对外 API 仍以 slug 字符串收发）。

    0027 起关联目标是 `institutions`（旧 `attributes(type='account')` 已删表）：
    账户维度在 ADR-0019 里收敛为机构，「名字即机构名」是 0026 过渡期的既有口径。
    """
    __tablename__ = "source_groups"
    __table_args__ = (
        Index("ix_source_groups_enabled", "enabled"),
        Index("ix_source_groups_sort", "sort_order"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(100), nullable=False, unique=True)     # 稳定标识（bantuou 等）
    label = Column(String(100), nullable=False, unique=True)    # 显示名（示例机构）
    asset_prefix = Column(String(100), nullable=False)          # 资产映射窄前缀模板
    guide = Column(Text, nullable=True)                         # 最佳截图指导（文本）
    guide_image = Column(String(500), nullable=True)            # 示例图地址
    rules = Column(Text, nullable=True)                         # 可选 JSON 解析规则
    enabled = Column(Boolean, default=True, nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    institutions = relationship("Institution", secondary="source_group_accounts",
                               back_populates="source_groups")


class SourceGroupAccount(Base):
    """来源组 ↔ 机构多对多关联（0027：原 `attributes(type='account')` 改指机构）。

    形状为代理主键 + `UNIQUE(source_group_id, institution_id)`（旧复合主键
    `(source_group_id, attribute_id)` 与 `value` / `created_at` 两列全库零引用，
    0027 重建时一并收窄）。表名沿用 `source_group_accounts`：它是关联事实表名，
    改名的收益只是措辞，代价是所有存量库要先重建。
    """
    __tablename__ = "source_group_accounts"
    __table_args__ = (
        UniqueConstraint("source_group_id", "institution_id",
                         name="uq_sga_group_institution"),
        Index("ix_sga_group", "source_group_id"),
        Index("ix_sga_institution", "institution_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_group_id = Column(Integer, ForeignKey("source_groups.id"), nullable=False)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)


class ParserVocabulary(Base):
    """全局解析词表的**增量覆盖**（一行一个词表名，只记用户显式改动的词）。

    生效词表 = `(builtin ∪ add_items) − remove_items`：出厂词永远在代码常量里，
    本表只存差额。存增量而不是整表替换，是因为整表替换下「某次保存时列表少了一项」
    ＝ 那个出厂词永久消失，要等到某天解析漏行才发现；增量只影响显式改动的词，
    未落库的键代表「全用出厂值」。

    `remove_items` 里的每个删除都**必须有理由**（写 audit_log，action=
    parser_vocabulary_update）——校验在服务层，本表只存事实（理由属审计，不属配置）。
    词表种类是个位数，整表只有几行，故除主键外不建任何索引。
    """
    __tablename__ = "parser_vocabularies"

    key = Column(Text, primary_key=True)                       # 词表名（如 ignore_words）
    add_items = Column(Text, nullable=False, default="[]")     # JSON 字符串数组
    remove_items = Column(Text, nullable=False, default="[]")  # JSON 字符串数组
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class SourceGroupLearnedRule(Base):
    """来源组的用户教学规则（从 `source_groups.rules` JSON 的三键拆出来的独立表）。

    取代旧 JSON 的 `rename[]` / `ignore[]` / `exclude[]`：同一列混着组级解析配置
    （layout/amount_header）与教学规则，既无法统计命中，也无法记录来源与时间。

    语义（与拆表前逐项等价）：`rename` 按 `sort_order` 级联改名（前一条的输出可被
    后一条继续改写）；`ignore` 子串命中即丢弃整行；`exclude` 严格全等、只在预览行上
    生效。匹配方式不统一是既有裁决，本表只换承载方式，不改语义。

    `replace` 仅 rename 使用；唯一性用**表达式索引** `COALESCE(replace,'')` 表达——
    SQLite 里 `NULL` 互不相等，普通唯一约束会放过「同 match 但 replace 都是 NULL」
    的重复行。

    `hit_count` / `last_hit_at` 是 P2（命中统计）的占位列，本期不写入，恒为 0/NULL。

    `rule_type='aggregate'`（ADR-0029 D7 / 票 03 D1）：该组的「汇总行词表」——命中
    这些词的 preview 行给 `aggregate_hint=true`（**预勾可取消**），行照常可见、只是
    不落账。语义与 rename/ignore/exclude 并列但**互不影响**：它不删行、不改名、不做
    清零候选过滤。
    """
    __tablename__ = "source_group_learned_rules"
    __table_args__ = (
        CheckConstraint("rule_type IN ('rename','ignore','exclude','aggregate')",
                        name="ck_sglr_type"),
        CheckConstraint("origin IN ('manual','import','migration')", name="ck_sglr_origin"),
        # 读取按 (组, 序) 排序：rename 的顺序即级联顺序，必须稳定
        Index("ix_sglr_group", "source_group_id", "sort_order"),
        # 唯一性口径 = (组, 型, match, replace)；replace 可空，故用 COALESCE 归一后再比
        Index("uq_sglr_group_type_match_replace", "source_group_id", "rule_type", "match",
              text("COALESCE(\"replace\", '')"), unique=True),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_group_id = Column(Integer, ForeignKey("source_groups.id", ondelete="CASCADE"),
                             nullable=False)
    rule_type = Column(Text, nullable=False)                  # rename / ignore / exclude / aggregate
    match = Column(Text, nullable=False)
    replace = Column(Text, nullable=True)                     # 仅 rename
    note = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)    # rename 靠它保序
    origin = Column(Text, nullable=False, default="manual")    # manual / import / migration
    hit_count = Column(Integer, nullable=False, default=0)     # P2 占位，本期不累加
    last_hit_at = Column(DateTime, nullable=True)              # P2 占位
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class ExpenseCategory(Base):
    """支出分类维度表（独立于 attributes，见支出录入契约 §3.1）。"""
    __tablename__ = "expense_categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False, unique=True)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class FixedExpenseTemplate(Base):
    """固定支出模板：周期经常性支出（房贷/订阅等），生成靠手动触发。"""
    __tablename__ = "fixed_expense_templates"
    __table_args__ = (
        Index("ix_fixed_expense_category", "category_id"),
        Index("ix_fixed_expense_account", "account_id"),
        CheckConstraint("cycle IN ('monthly','quarterly','yearly')",
                        name="ck_fixed_cycle"),
        CheckConstraint("amount > 0", name="ck_fixed_amount"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)  # 预期金额，正数
    cycle = Column(String(20), nullable=False)  # monthly / quarterly / yearly
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)
    category_id = Column(Integer, ForeignKey("expense_categories.id", ondelete="SET NULL"),
                         nullable=True)
    account_id = Column(Integer, ForeignKey("institutions.id", ondelete="SET NULL"),
                        nullable=True)  # 须为 institutions 行（0027：原 attributes(type='account')）
    note = Column(String(255), nullable=True)
    active = Column(Boolean, nullable=False, default=True, server_default=text("1"))
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class Expense(Base):
    """支出流水（固定 + 临时大额）。

    手动记录 template_id/period 均为 NULL；SQLite 唯一约束对 NULL 不参与
    去重，幂等仅约束生成记录 (template_id, period)。支出为流水，不参与净值计算。
    """
    __tablename__ = "expenses"
    __table_args__ = (
        UniqueConstraint("template_id", "period", name="uq_expense_template_period"),
        Index("ix_expenses_expense_date", "expense_date"),
        Index("ix_expenses_category", "category_id"),
        Index("ix_expenses_account", "account_id"),
        CheckConstraint("expense_type IN ('fixed','large_oneoff')",
                        name="ck_expenses_type"),
        CheckConstraint("amount > 0", name="ck_expenses_amount"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    expense_date = Column(Date, nullable=False)
    expense_name = Column(String(100), nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)  # 正数存储
    expense_type = Column(String(20), nullable=False)  # fixed / large_oneoff
    category_id = Column(Integer, ForeignKey("expense_categories.id"), nullable=True)
    account_id = Column(Integer, ForeignKey("institutions.id"), nullable=True)  # 须为机构
    note = Column(String(255), nullable=True)
    template_id = Column(Integer, ForeignKey("fixed_expense_templates.id", ondelete="SET NULL"),
                         nullable=True)
    period = Column(String(7), nullable=True)  # YYYY-MM，仅生成记录携带
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class AuditLog(Base):
    """审计日志（v5 步骤 4，不变量 8 承载物）——**append-only 只读映射**。

    写入仅经 services.audit_service.record()（与业务写入同事务）；
    存储层由 0009 触发器拒绝 UPDATE/DELETE；本映射不建任何 relationship，
    杜绝反向删除路径。prev_hash 为三期 hash-chain 链接值（前一行的 row_hash；
    首行＝GENESIS；链尾另存 audit_chain_state.last_hash）。
    before/after 是冗余快照（不作真源）：数据内痕迹由 superseded_by/显示名史承载。
    """
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_entity_row", "entity", "row_id"),
        Index("ix_audit_occurred_at", "occurred_at"),
        Index("ix_audit_evidence", "evidence_ref"),
        Index("ix_audit_request", "request_id"),
        CheckConstraint("actor IN ('owner','system')", name="ck_audit_actor"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    occurred_at = Column(Text, nullable=False)      # ISO 字符串
    actor = Column(Text, nullable=False)            # owner | system
    entity = Column(Text, nullable=True)
    action = Column(Text, nullable=True)
    row_id = Column(Integer, nullable=True)
    before_json = Column(Text, nullable=True)       # NULL＝创建
    after_json = Column(Text, nullable=True)        # NULL＝删除
    evidence_ref = Column(Integer, ForeignKey("screenshot_evidence.id"), nullable=True)
    request_id = Column(Text, nullable=True)        # contextvars 拾取，关联 HTTP 日志
    idempotency_key = Column(Text, nullable=True)   # 步骤 5：每用户动作去重键（与 request_id 并列落审计）
    prev_hash = Column(String(64), nullable=True)   # 三期 hash-chain：前一行 row_hash；首行 GENESIS
    # ADR-0027 审计留痕契约（0036）：变更原因。两列均可空——NULL＝「未记录原因」，
    # 存量行不倒填；**不进 audit_chain.FIELDS**，故不改 hash-chain 口径。
    reason = Column(String(20), nullable=True)      # correction|evolution|normalization|import|migration|other
    reason_note = Column(String(200), nullable=True)  # 一句话说明（≤200，应用层校验）


class AuditChainState(Base):
    """审计链单行状态（三期-3 W1，ADR-0013 D10；迁移 0018）。

    `id=1` 恒单行：`last_id/last_hash/row_count` 由插入路径 CAS 更新
    （`WHERE last_id=:expected`，失败→409 AUDIT_CHAIN_CONFLICT）。
    """
    __tablename__ = "audit_chain_state"

    id = Column(Integer, primary_key=True)
    algorithm = Column(Text, nullable=False, default="sha256.v1")
    last_id = Column(Integer, nullable=False, default=0)
    last_hash = Column(String(64), nullable=False)
    row_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(Text, nullable=False)


class AuditChainSeal(Base):
    """审计链日封存（三期-3 W1，ADR-0013 D10；迁移 0018）——append-only。

    `prev_seal_hash` 自链（首封＝GENESIS）；`seal_hash` 同时写应用日志与备份
    `manifest.audit_head`，用于发现整库回退（尾部截断）。
    """
    __tablename__ = "audit_chain_seals"
    __table_args__ = (
        UniqueConstraint("sealed_date", name="uq_audit_chain_seal_date"),
        Index("ix_audit_chain_seals_sealed_at", "sealed_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sealed_date = Column(Text, nullable=False)      # YYYY-MM-DD（单日一条）
    sealed_at = Column(Text, nullable=False)        # ISO 时间
    algorithm = Column(Text, nullable=False, default="sha256.v1")
    last_id = Column(Integer, nullable=False)
    last_hash = Column(String(64), nullable=False)
    row_count = Column(Integer, nullable=False)
    prev_seal_hash = Column(String(64), nullable=False)
    seal_hash = Column(String(64), nullable=False)


class LLMProvider(Base):
    """LLM 供应商配置（截图 AI 一致性校验用）。

    全标量字段；api_key 明文存本地 SQLite（单机自用，威胁模型接受），
    任何 API 出口必须经 _serialize_provider 脱敏，绝不回传明文。
    kind 取值：deepseek | dashscope | openai_compatible。
    建表靠 lifespan 的 create_all；seed 靠 db_migration_service.seed_llm_providers。
    """
    __tablename__ = "llm_providers"
    __table_args__ = (
        Index("ix_llm_providers_enabled", "enabled"),
        Index("ix_llm_providers_sort", "sort_order"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(100), nullable=False, unique=True)   # 显示名（DeepSeek 等）
    kind = Column(String(50), nullable=False)                  # deepseek|dashscope|openai_compatible
    base_url = Column(String(500), nullable=False)
    api_key = Column(String(500), nullable=False, default="")  # 明文本地存储
    model = Column(String(200), nullable=False)
    is_default = Column(Boolean, default=False)                # 全表至多一行 True
    enabled = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    # —— 最近一次连通性测试结果（0034，spec llm-provider-status D1）——
    # 四列一律可空且无列级默认：NULL ＝「从未测试」，与 False（测试失败）语义不同，
    # 因此 last_test_ok 绝不能给 default=False。
    last_tested_at = Column(DateTime, nullable=True)           # 探测完成时刻
    last_test_ok = Column(Boolean, nullable=True)              # True 成功 / False 失败 / NULL 未测
    last_test_message = Column(String(300), nullable=True)     # 结果摘要（成功的回包或错误文案）
    last_test_elapsed_ms = Column(Integer, nullable=True)      # 本次探测耗时（毫秒）

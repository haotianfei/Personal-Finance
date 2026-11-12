# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/) 思路：主版本表示架构世代（V1 记录式模型 → V2 快照式资产/负债模型 → V3 截图 OCR 智能识别 → V4 AI 辅助 → V5 身份与可信底座），小版本随功能里程碑递增，修复递增补丁版本。v2.11.0 为 2026-08-19 自既有部署导入的基线，v2.12.0 起为本仓库里程碑；v3.0.0 起主版本号随世代重定。

## [Unreleased]

### 新增

- **资产项抽屉内可直接新建标签**（tag-create-in-sheet，票 01–04）：此前给资产打标签必须先跳到「设置 → 字典 → 标签」建好再回来勾选，抽屉里的选择器只能挑选。现在在抽屉的标签搜索框输入一个不存在的名字，候选**首项**就是「+ 新建标签「X」」，回车或点击即创建并自动勾上——但**只有这个显式动作才会创建**（输入本身永不落库，D8），保留「不静默新建、标签不退化成自由文本」这条老口径。新建只进草稿，随「保存到资产项」一起落库并提示「已新建标签，保存后生效」；中途放弃则在字典里留下 0 引用的自定义标签（不自动清理）。撞名不会报错：本地已有同名直接复用并提示，外部并发抢先建成（409）则重取标签后按精确名匹配再勾上；已满 5 个时不出现新建项并提示先取消一个。名称口径两侧拉齐：trim 后上限 20 字，后端 `TagCreate.name` 补 `max_length=20`（此前只有前端 `maxLength`，后端完全不校验长度），超长 422 且消息带「20」。无迁移、无新增端点（复用 `POST /api/classification/tags`），标签创建审计不区分来源。
- **当前值视图回显项级备注**（current-value-note，票 01）：`GET /api/assets/current` 每行回传 `note`（一次 IN 批量取数防 N+1，trim/空串归一 `NULL`，`include_archived=true` 时归档项同样覆盖）；当前值表行上便签图标（悬停看全文、空备注不显示）＋「进入资产项」抽屉入口，复用既有的 `AssetItemSheet` 与同一份开关状态，不新增写入口。无迁移、无新增端点。
- **资产记录元数据落项级：备注 + 单条打标走「进入资产项」，分期编辑两入口退役**（asset-record-edit，票 06–14，ADR-0031）：标签此前只能开批量弹窗打、备注字段根本不存在，而「编辑记录」弹窗对着 write-once 的账本行——改金额只能走更正追加行。现在记录行的资产名称可点，或在操作列点「进入资产项」，右侧抽屉一次摆出该资产的 N 条历史、最新一期金额与分类，再给备注与标签两个写入口：备注是项级单条文本（`asset_items.note`，迁移 `0039`，500 字上限，trim 后空串归一 NULL，`PUT /api/asset-items/{item_id}/note`，审计 `note_update` traced，**不进** `asset_item_attr_history`），标签沿用既有 ≤5 自由标签，两者一次改完覆盖该资产全部历史（抽屉里写明「改的是资产项，不是这一条记录」，且多期金额不相加、只显示最新一期）。列表接口补回 `note` / `updated_at`，抽屉读备注一次请求（此前无读接口，靠审计日志派生，已消除）。分析页维度下拉新增「标签」，与「未打标」派生行同屏（`get_proportion_by_tag` 用 `NOT EXISTS` 独立取数，不掺进三表 join）——标签可重叠，各标签之和可超 100%，故界面**不显示**「合计 100%」、「各标签之和」与「按项并集」也分别标注，且标签维度不进配置目标与预警（要求各档加总 100% 的语义与多值互斥）。
- **待录入清单的父子层级看得见了：pstree 车道引导线 + 行内开关 + 归父即汇总**（review-tree-visual，票 01–07，ADR-0029 §6）：截图复核页的父子关系以前只有 `paddingLeft` 缩进，第二级往下分不清谁是谁的孩子。现在每级一条 16px 车道，祖先位置画贯穿竖线、本行位置画 `├`/`└` 并把横折接到名称左缘——第一级用 2px 品牌色、更深的层级用 1px `#cbd5e1`，折叠后不留残端（`TreeRow` 新增 `isLast` 与 `ancestorHasNext[]`，末位判定看穿子树）。同批收掉三处老毛病：① 表头第 2 列写着「勾选」却放的是「跳过/汇总行」，实际没有全选也没有批量勾选——整列撤销（8 列 → 7 列），两个开关改成图标 + tooltip + `sr-only` 全称挪到名称行尾靠右，「存为忽略 + 备注」落在名称列第二行；② 身份确认黄框原来横跨全部列（`colSpan={8}` 次要行），现在进名称单元格、宽 100%，候选超 3 条折叠成「另有 N 个候选，展开」，底部摘要退化为「还有 N 行需要确认身份」并可一键展开含待确认的父行；③ 汇总行金额下方直接挂差额标记（`data-aggregate-diff`，已对上/已接受/差 ±¥X/子项待确认四态，点开看「父 ¥A − 子 ¥S」并跳去核对），「金额差异」列对汇总行留空不再显示无关的本行基线差。归父从两步变一步：拖行首手柄或选「归到…」时目标行**自动成为汇总行**（来源标记 `manual`/`rule`/`parent`），最后一条子边解除只自动撤「归父自动」那一种；父行还有子项时手取消「汇总行」被阻断并提示「先解除子项归父」，另勾「跳过」的子行不计入子项合计（差额照常暴露）。纯前端改动，无迁移、无接口改动。
- **三环境分层：dev 一键起停、staging 预发、生产上线走门禁**（env-tiers，票 01–07）：此前只有「改源码」和「直接上生产」两级，上线靠手敲 `package.sh → deploy.sh → smoke.sh`，失败只能靠记忆里的旧镜像 ID 回退，且开发/预发端口都落在浏览器与 Next 共同的受限段（6665–6669），`next dev -p 6666` 直接被拒。现在端口统一重排为「后端尾 8 / 前端尾 9」——dev `6678/6679`、staging `6688/6689`、prod `6698/6699`（生产已于 2026-11-09 14:38 由 `6789/6788` 切到 `6699/6698`，版本 `20261109-aa43d66`，smoke 6/6）。`scripts/dev-up.sh` 起后端 reload + 前端 dev（固定 `data/person_fin_dev.db` 与 `DB_ROLE=dev`，pidfile 落本机临时目录），`dev-down.sh` 先认容器绝不误杀；staging 用生产镜像 + 生产库副本（`STAGING_*_IMAGE` 专用 tag，`staging-refresh.sh` 走 backup API 取一致快照并对拍行数）；生产上线只走 `deploy/scripts/release.sh`——门禁 → 给现役镜像打 `rollback-*` 回滚点 → 双 tag 构建 `:<日期>-<shortsha>` + `:local` → 上线 → healthy + smoke → 写 `deploy/RELEASES.md`，④ 之后任一步失败自动回滚并标 FAIL。门禁收敛为 `scripts/ci.sh` 单入口（默认档 880 秒 pytest 全量 + tsc + lint + 文档/安全两道检查，`--fast` 60 秒，`--with-probe` 追加真浏览器探针），`install-git-hooks.sh` 装 pre-push。备份改为「tar + 保留策略」（日留 14 天、每月 1 号留 6 个月，默认 dry-run）配 systemd user timer（2026-11-09 已安装并 enabled，每日 03:00）；新增季度恢复演练 `scripts/restore_drill.sh`——解备份到隔离项目 `person_fin_drill`，对拍行数、`schema_version` 与审计链 `chain/verify`，报告落 `.scratch/env-tiers/drills/`（2026-11-09 首跑 PASS）。
- **开发库与部署库不再同名：库角色四档 + 默认路径改 `person_fin_dev.db`**（db-dev-suffix，票 01）：此前开发副本和 `deploy/data/person_fin.db`（唯一在线库）都叫 `person_fin.db`，一条路径打错就可能对着线上库跑脚本。现在开发侧默认库改为 `data/person_fin_dev.db`，启动日志第一行就打 `db_path=` 与 `db_role=`（`dev`/`staging`/`prod`/`unknown`，按文件名推导，`DB_ROLE` 可覆盖）；`scripts/sync_deploy_db.py` 默认目标同步改 `_dev` 并在 dry-run 打印两侧角色；部署侧 `deploy/**` 一字未动。
- **记录明细默认按更新时间排序，并把更新时间显示成一列**（records-updated-at，票 01/02）：想找「刚改过的那条」以前只能按日期、名称或金额排，得翻页去猜。现在记录明细默认按「更新时间」倒序，金额右侧常显一列更新时间（`2026/11/08 02:10`，悬停显示原始时间串），点表头可切升序。排序在服务端做，且**所有显式排序都追加固定的 `id` 倒序兜底**——现役库里 42% 的行（1478/2605）挤在 28 个重复时间戳上，最大一组 1103 行完全同刻，没有兜底键时同刻行跨页会重复或漏掉。口径要记牢：更新时间是该行**最后写入**的时刻，不等于「你手动改过」——分类、标签、归属、流动性写的是资产项，不会刷新记录行；改名选「历史回写」会把该资产项名下全部历史快照一起刷成当前时刻。当前值视图、接口响应字段、有无索引一律未动（表 2605 行，无索引排序仍够快）。顺带收掉一条无效请求：页面级账本查询改为按视图门禁（`enabled: view === 'ledger'`），切到当前值视图时不再白发一条 `GET /api/assets`（它的读模型本来就是 `/api/assets/current`）。
- **多页截图录入：汇总行只核对、明细行落账**（multi-page-aggregate，ADR-0029）：一张截图里的「贵金属」往往是另几张截图里几个子项的合计，此前两页都按独立资产项入账，净值和差异全被抬高。现在每行可勾「汇总行（只核对，不入账）」——命中汇总词表默认预勾、可取消，勾中后金额变灰、徽章显示「核对」，且**完全不写库**（不写资产行、不改显示名、不沉淀别名、不动血缘）；与「跳过」互斥，与身份裁决互斥（转汇总时自动撤销裁决并提示），汇总行也不能当 bind 目标。归父有两条路径：「归到…」选择器是**键盘主路径**（`/` 聚焦、↑↓、Enter、Esc），行首手柄拖拽只作加速器，悬停 500ms 才允许投放、拖到自身或后代替非法。底部核对面板逐条显示 父项金额 / 子项合计(直接子项) / 差额，零容差：差额≠0 必须填理由才能「接受差额」，接受后差额再变则自动作废。提交改三段：先建批次 → 逐图 apply 带 `batch_id` → `finalize` 统一校验（缺成员 409 `BATCH_NOT_COMPLETE`、差额未接受 409 `RECONCILE_MISMATCH`），任何一段失败都不做跨图回滚，重试沿用同一批次。计量口径同步改成**叶子计量**：有子项的父项退出净值、占比、告警与导出合计，列表与详情照常显示并标「汇总项 · 不计入合计」，趋势仍画父项曲线但不进总额线，解除构成后自动恢复。数据层：迁移 `0037` 建 `item_compositions`（一子一父、禁自环成环、`UNIQUE(child_item_id)`）与批次四表，`0038` 给来源组学习规则加 `aggregate` 类；护栏用例锁死「父 100 + 子 60 + 子 40 同日同 active → 净值 100」。
- **删除与归档必须说明「为什么」：理由从可选转强制**（audit-reason-enforce，ADR-0027 D7-a）：上一批打通了理由通道，但填不填凭自觉——`KNOWN_DEBT` 里 7 条删除路径永远拿不到理由，`reason` 列对它们等于不存在。现在把强制点收在**写库单点**：`actor='owner'` 且动作属于纠正类（`correct`／`delete`／`archive`）而缺 `reason` → **422 `AUDIT_REASON_REQUIRED`**，业务与审计同事务回滚（零写入、零留痕），前端把这句译成人话并**请求根本不发**（弹窗不关、已选理由不丢、焦点回下拉），422 只作兜底。`actor='system'` 一律放行——**该改身份的不伪造理由**：备份保留策略的清理从「前端循环调删除接口」迁到后端调度器（`actor='system'`、`reason='normalization'`、`after.source='retention'`），目标激活时隐含的旧版归档同理（`source='activation'`）；让人冒充填理由是留假证据，比没理由更糟。为每个删除/归档入口补上理由输入：告警规则、字典五页（共用 `DictionaryCrudCard` 一处接入、四页自动获得）、凭证单条与批量清理、目标归档两处；批量操作**头行与每条明细同理由**（此前「只记头行」会让明细行被自己的强制点 422 掉，属设计自伤，已在 spec 记修正）。备份文件删除是唯一的豁免：它不向用户索要理由，由服务端记 `normalization` 并用 `after.source` 区分手动清理与保留策略。`KNOWN_DEBT` 债务清单**清零并删除**，改为「owner 侧纠正类审计行不得缺 reason」的正向断言。存量 11 行 NULL 不回填（语义仍是「当时没记」）。
- **截图复核页可直接看原图**（image-viewer）：待录入清单每张图的**文件名与缩略图**都能点开查看器——大图铺满、0.5–4× 缩放与拖动、多图左右切换、双击复位、「下载原图」保留原始文件名。此前核对识别结果得先离开页面去凭证列表翻同一张图，多图批量录入时几乎不可用。原图全部走既有 `GET /api/screenshots/evidence/{id}/image`（realpath 越界校验 + 一天私有缓存，`?download=1` 走附件），**后端零改动**；无 `evidence_id` 的旧会话回退本地 blob 预览，不报错。键盘可用（Esc 关闭并回焦、←/→ 翻页、± 缩放），并保留「新标签打开」兜底。不做 OCR 坐标定位（preview 契约无坐标框，属另一量级）。
- **纠正类操作可以说明「为什么改」**（audit-reason-capture）：审计行早有 `reason` 列，但没有任何地方能填——`reason` 一直是 NULL，取证时「为什么删这条」「为什么改金额」只能靠猜。现在删资产、批量删除、值更正三处在操作时可选一个原因（`correction` 旧值写错 / `evolution` 情况变了 / `normalization` 表述归一 / `import` 导入 / `migration` 迁移 / `other` 其他）并附一句 ≤200 字说明；不填也能操作，只是这次不记录原因（NULL 表示「当次未填」，与「填了空」不同）。理由走 query（`?reason=&reason_note=`），六类端点同形：`POST /assets/{id}/correct`、`DELETE /assets/{id}`、`POST /assets/batch-delete`、`POST /asset-items/{id}/archive`、`POST /asset-items/{id}/owner`、`PUT /asset-items/{id}/classification`；批量把理由记在**批次头行**，逐行明细不带（一次操作一个理由，不是每行各编一个）。改名端点不加新参数——它早就有 `reason`/`effect`，现在顺带把 `reason` 映射进审计行、`reason_note` 透传。非法枚举与超长说明一律 422 且不落任何审计行。前端把理由输入做成共用组件（下拉 + 选后才出现的说明框 + 实时字数），三处共用一份、按目标对象保留草稿，Esc 误关不清已填内容。仍未转成必填：现有调用点与前端请求体没有该字段，强制就是 500——缺口以 7 条显式债务清单登记（见 `tests/test_audit_contract.py`）。
- **全局操作日志画出「本代起点」分代线**（audit-best-practice，票 04 延伸）：空库重建会换掉整条审计链，日志页此前把两代的行平铺在一起，看不出「这条属于重建前还是重建后」。现在遇到 `schema/rebuild_import` 行就画一条分隔条：上边是当前库代，下边是上一代，右侧写明重建时间与「导入 N 行资产」、来源只显示文件名；该行仍然占一条计数、不可点不可展开。没有重建行时页面与之前完全一致。
- **取证视图：从「这个数不对」点到「谁在什么时候改的、原来的值、凭证在哪」**（audit-best-practice，票 02）：审计链此前一直「只能写、不能看」——`audit_log` 里躺着 15 行、`frontend/src` 里 `audit` 命中 0 处，取证等于翻数据库。现在记录页每行多一个「操作历史」抽屉，把**两类来源合成一条时间线**：`audit_log`（操作记录，人做过什么）与四张痕迹表 `asset_item_attr_history`／`asset_item_owner_changes`／`principal_changes`／`asset_item_names`（数据自述，账本自己记着什么）。三分术语写死在界面与文档：**操作记录 / 数据自述 / 曾用名**——同一次改名会同时出现在两边，故同 `kind` 且相隔 ≤2 秒的痕迹行让位给审计行，只留一条。合并、去重、排序与游标分页全在后端（`GET /api/asset-items/{item_id}/history`，`limit`+`cursor`+`sources`，默认 50 条最新在上），前端只渲染，两处界面因此不会各写一套合并逻辑。每条形如「旧 → 新」只显变化字段（新建/删除显全集），`request_id`、`idempotency_key`、`prev_hash` 前 8 位收进「技术细节」默认折叠；`actor` 显示为「我 / 系统」。凭证详情新增「受影响资产项」反向入口，点开是**同一抽屉**（换时间线而非叠第二层），弹层关闭后下层滚动位置与选中项原地不动。设置页新增「操作日志」轻量全局页：entity/action/actor/日期区间/请求号/幂等键筛选 + 五值链状态徽标（`match` 绿、`db_newer` 蓝、`db_older` 琥珀、`diverged` 红、`unknown` 灰，吃 `GET /api/audit-log/integrity`），提示句统一「只能发现不一致，不做拦截」。`GET /api/audit-log` 同时补 `actor`、`order`、`idempotency_key` 三个筛选（幂等键七天后过期，旧行仍按原值留存，故可能查不到键表里的那条）。
- **整库回退能被发现：库外锚对账 + 恢复前硬阻断**（audit-best-practice，票 03，ADR-0028）：审计链能发现「行被改」，却一直发现不了**「整库被换成旧的」**——链是自洽的，因为整个库连同链头一起回退了；此前唯一的线索是备份 `manifest.json` 里的 `audit_head`，而它**只写不读**。现在每份备份的 `audit_head` 成为「库外锚」：备份目录里所有 `manifest*.json`（含 bundle 内的 `manifest.json`）被逐一与当前库链头比，判定五值 `match` / `db_newer` / `db_older` / `diverged`（行数相同而链头不同＝同一长度上的另一条链）/ `unknown`（锚里没有 `audit_head`，或只有链头不同却缺行数——**没对过账不等于账对**）。三个时机：启动后（只 `logger.critical`，**绝不阻断启动**——单用户系统没有值班人，锁死门外比漏报更糟，那样连告警都没人看得见）、恢复替换前（**唯一的硬阻断**：目标包比当前库旧或分叉即 409 `AUDIT_ROLLBACK_DETECTED`，要越过必须显式 `confirm_rollback=true`）、日封存后。只读端点 `GET /api/audit-log/integrity` 给出 `head`／`seals{last_seal_id,max_id,lag}`／逐锚判定／`worst_status`（没有锚就是 `unknown`，不报平安）；响应与日志只出现文件名、行数与哈希前 8 位，不出绝对路径。被拒绝的恢复**在盘上不留任何新文件**（对账排在造 rollback bundle 之前）；越过阻断时恢复留痕的 `after` 记 `integrity` 与 `rollback_confirmed`（真的越过了才是 true），不加列、不进链哈希。
- **重建不再是「审计断代」：旧链导出 + 新库一条代际行**（audit-best-practice，票 04）：2026-11-04 的空库重建把 `audit_log` 当运维台账丢弃，结果 2,595 行资产的创建过程零留痕——重建管道自己成了审计的洞。现在重建收尾多一步：先把旧库审计链只读导出成 `audit-chain-<时间戳>.jsonl`（逐行原样、按 id 升序、全部物理列）＋`audit-bridge-<时间戳>.json`（旧库行数／链头／封存条数／JSONL 的 sha256 与字节数，另附 `algorithm`、`source_chain_ok`、`source_state` 供判读旧链是否自洽），落在重建产物目录；再往**新库**写一条 `schema/rebuild_import`（`assets_imported`／`source_rows`／`source_head` 与 `source_seals`／桥文件名与 sha256 前 16 位／脚本名与版本）。新库因此有了「我是从哪条链、多少行、哪个时刻接过来的」的第一手记录，第三方拿 JSONL 用同口径可逐行复验。**只写头行不逐行**（重建是整库代际切换，不是对账本操作）；`source_label` 只允许文件名，绝不含绝对路径、主机名与真实金额；桥导出失败**不阻断重建**（旧库没有审计表、写盘失败都只 warning 并在 `report.json` 留 `audit_bridge.error`），跳过时仍写留痕行并标 `bridge_skipped`——重建零留痕正是这一步要修的病，不该因为缺旧库就复原。
- **审计留痕契约：131 个写端点逐个定性，漏写会在测试里变红**（audit-best-practice，票 01，ADR-0027）：此前的判定口径只有一句「任何写入必须留痕」，于是实测 131 个写端点里只有约 20 个真在写——**账本批量删除的 docstring 声称有审计、代码却是无声硬删**，导入、恢复整库、分类字典、密钥轮换全部不留痕，`entity`/`action` 是自由字符串（54 处调用点里只有 2 个硬编码字面量），无从枚举、无从校验。现在把「该不该留痕」写成五类桶的可套用规则（**值**：账本数字及其行；**口径**：改变同样数据如何被分组的元数据；**出处**：凭证与其识别结果的采纳与更正；**离场**：数据离开系统；**整库**：这库是哪一版），收口为 `services/audit_contract.py` 一张契约表：131 行逐条给出桶、`entity`/`action`、理由与状态，现状 **105 traced / 26 exempt / 0 planned**；`ENTITY`/`ACTION` 成为受控枚举，`make_change()` 遇未登记值直接抛错。守卫测试双向比对契约表与 `app.routes`：**未登记的路由、以及登记了却不存在的路由，两个方向都会红**——这才是「以后不会再漏」的实际保障。豁免只有两类且必须登记（预览任务簿记、`recognition_json` 派生回写），新增豁免＝改契约，不是「忘了写」。写库层同时补三件事：`audit_log` 加 `reason`/`reason_note`（迁移 `0036`，两列不进链哈希 `FIELDS`，故链口径一字未动、存量行留 NULL 表示「当时没记」）；`before/after` 走单点脱敏——`api_key`/`token`/`secret`/`password` 类字段一律落 `"<redacted>"` + `_fp`（sha256 前 8 位）+ `_len`，原文永不入库（LLM 供应商轮换密钥因此可以留痕而不必担心不可删的行里躺着明文）；批量写入统一「1 条批次头（`batch/execute`，`after={op,count,ids_hash,scope}`）+ N 条逐行明细」，界面型批量单次上限 500 行、超限 400 `BATCH_LIMIT_EXCEEDED`，导入按文件整体不设上限。三处时序陷阱单独立规矩：恢复/换库的审计行必须写进**替换后的新库**并携带旧库链头（否则会被整库替换带走）；备份文件的创建先落盘再记账、删除先记账再删文件；`reason` 只对纠正类「应该有」，运行时缺 reason **不报错**（现有前端没有这个字段，强制就是 500），缺口以 10 条显式债务清单记录并逐票清零。
- **识别行关联到已有项（bind）**（asset-name-identity，票 01，ADR-0026）：截图复核页每行新增「关联到已有项」——识别出来的名字与库里某个资产项其实是同一样东西时，直接把这一行记到它名下，而不是另开一项。与既有「沿用旧项（合并）」的关键区别是**不改身份**：不建后继项、不归档前身、不写血缘，`asset_items.id` 与既有历史行一行不动；该行识别名沉淀为目标的**别名**（`asset_item_names.origin='alias'`，不带生效日），下次 OCR 再读成这种写法仍能认回同一项。同时把「静默新建」堵死：所有拟新增行必须显式表态（新增 or 关联），未表态即整批 400 `identity_decision_required` 且零写入——只读取证显示现役库 active 项之间 66 对近似命名里有 61 对（92.4%）双向都匹配不上，此前正会被静默建成两个项；「未识别项」清零策略也会跳过已被关联的目标（`zero_skipped_due_to_bind`），不会一边关联一边把当天金额清零。
- **改名留痕：原因 / 影响范围 / 历史回写两步确认**（asset-name-identity，票 03，ADR-0026）：改名从「改个显示名」变成一次要留痕的裁决。请求必须带 `reason`（`correction` 旧名写错 / `evolution` 名称演进 / `normalization` 书写归一）与 `effect`（`current` 只改当前名 / `history_backfill` 连同历史快照一起回写）；`correction` 默认回写、其余默认不回写，选非默认组合必须在备注里写明理由。回写是对「改名永不回写历史」的有条件破例，因此拆两步：`POST /api/asset-items/{id}/rename/preview` 先返回影响面（将影响多少行、日期跨度、其中多少行已被取代）并发一次性令牌（10 分钟、绑定请求载荷与影响面哈希），确认后 `POST .../rename` 带令牌执行；预览之后该资产项的历史行有变动就 409 要求重新预览，同一令牌只能成功一次。撞名判定改为归一后比较**三层名**（其他项的现名 / 历史名 / 别名），自己的旧名不算，故撤销改名就是再改回旧名。迁移 `0035` 重建 `asset_item_names`：扩 `reason`/`effect`/`reason_note` 三列并把 `origin` 扩到 `alias`，存量 146 行原样搬运、原因列留 NULL（语义是「当时没记」）；重建后自检行数、列形状、索引与 append-only 触发器，任一不符即中止启动。
- **凭证详情可见识别快照**（evidence-recognition-view，票 01）：详情弹层新增「识别快照」一节——引擎 / 来源（服务端 OCR vs 前端 AI）/ 识别时间 / 解析器版本 / 识别与采纳行数，全部取自列表已下发的摘要，**打开抽屉零额外请求**；点「查看识别行」才按需拉 `GET /api/screenshots/evidence/{id}/recognition` 取完整快照（识别行与已采纳行逐条列名称金额，原始块只报条数），折叠再展开吃缓存不重复请求。此前快照虽然一直在存，却没有任何界面能看——「日后对拍识别率」实际等于「日后翻数据库」。
- **识别结果回写端点**（evidence-recognition-writeback，票 01）：`PATCH /api/screenshots/evidence/{id}/recognition`。起因是时序：AI 识别由前端在 preview job **之后**发起，提交 job 那一刻手里还没有识别行，于是「只预览不采纳」的会话快照里 `recognized.items` 恒为 null，识别率无从对拍。回写端**只写 `recognized` 段**——`adopted` 段（人实际落账了什么）永远只归 `/apply`，两段必须能分开对照；重复回写按段覆盖、天然幂等，不写审计（快照是旁证不是业务操作）。失败一律降级：行不存在 404、缺字段 422，体积超限降级后照存，写库异常仍回 `200 + saved=false`（调用方是旁路回写，不该把识别流程判成失败）。前端在 `runAiRecognition` 拿到 `ai_only` 且判定为 `judged` 时静默补写；`unjudged`（零产出/无供应商）**不写**，否则会把「没结论」冒充成「识别出 0 项」。
- **预览任务回传识别行**（preview-job-recognition，票 01，ADR-0023 D7）：`POST /screenshots/preview-jobs` 增可选表单字段 `recognition`，与 `/preview`、`/apply` 同构同源（同一个 `parse_client_recognition`），并随 job `payload` 持久化——重启续跑时从 payload 取回，否则重排后的快照会退回 null，等于白跑。非法 JSON **只记 warning 不报错**：预览是主路径，补录性质的信息不合法绝不能让整张图识别不出来（与 `/apply` 的严格 422 刻意不同）。
- **凭证按来源组去重 + 复合唯一索引**（evidence-dedup-index，票 01，ADR-0024）：查重范围从「全局按 sha」收窄为「**同组**按 sha」，存储层补 `ux_screenshot_evidence_sha_group(file_sha256, source_group_id)`。迁移 `0033` 先删存量重复行（判据从严：同组内还有同 sha 的另一行 **且** 四类引用全为 0；每组保留引用最多者，并列取 id 最小；删前 log id 清单、删后行数对账），再建索引；**只删行不删文件**（事务管不了文件系统，回滚后行回来了文件却没了比留孤儿更糟），孤儿由 `--report-orphans` 报告、业主备份后删。删完仍有同组同 sha（例如多行都被引用）时**抛错点名到 sha 与组**，不把 `UNIQUE constraint failed` 甩给启动链。
- **凭证识别快照留存（可复核、可对拍）**（evidence-naming，票 03，ADR-0023）：每次识别都在凭证行落一份 `recognition_json`——`recognized`（机器读到了什么）与 `adopted`（人改后落账本的是什么），附 `parser_version` 与 OCR 块原文。日后「换规则后识别率掉了没有」不再只能靠肉眼：拿同一张图重跑，与存档逐行对拍即可归因。落库收口在**唯一写入函数** `record_recognition()`，三个写入点（`/preview`、异步预览 job、`/apply`）只许调它，不各写一份 JSON 组装。AI 路径服务端不跑 OCR，故识别行由前端回传（`POST /apply` 的可选表单字段 `recognition`，未传则记明 `items:null` 而不是假装存过）；体积超 1 MiB 先丢 `blocks` 打 `blocks_dropped` 标记，仍超只留条数——**存证优先，宁可少存也不因体积失败**。
- **存量凭证改名与元数据回填脚本**（evidence-naming，票 04）：`backend/scripts/rename_evidence_files.py`（默认 dry-run）把 28 条历史凭证改成新命名并回写 `file_path`，`backend/scripts/backfill_evidence_image_meta.py` 给 `image_meta IS NULL` 的行补体检表。两者都能对副本跑（`--db` / `--evidence-root`），失败即停并打印已处理清单，二跑幂等。改名有六态判定（rename / noop / missing / conflict / sha_mismatch / unsupported_ext），其中「盘上 sha 与库里 `file_sha256` 不符就跳过」这道门是为了**不让文件名撒谎**。
- **凭证详情展示原图体检表**（evidence-naming，票 05）：详情弹层补「尺寸 / 拍摄时间 / 设备 / 文件大小」四行——在哪个平台截的、图有没有被压缩过，看一眼就知道；缺值统一落「—」，不吐 `undefined`。
- **凭证文件命名规范化与原图体检表**（evidence-naming，票 02，ADR-0022）：新存证的文件名改由服务端生成 `<上传时刻>_sg<来源组ID>_<sha256 前 12>.<ext>`，客户端原名不再进入磁盘路径——此前 28 条凭证里 23 条的文件名是 `shot_upload_xxxxxxxx_abc123def456.jpg` 这种临时名（`tempfile.mkstemp` 的产物），既不表达含义又随客户端命名漂移。命名基座只用不可变字段：上传时刻（本地时区，精确到秒）+ 来源组自增主键（**不用会改的 slug**）+ 内容哈希；目标名撞车且内容不同则停止（不覆盖、不静默加后缀），同名同内容视为幂等复用。同时落 `image_meta`：`width/height/bytes/format` + EXIF 白名单（`DateTimeOriginal`/`Make`/`Model`/`Software`/`Orientation`，**GPS 子 IFD 从头到尾不访问**），Pillow 读不出或未安装时降级为 NULL 并记 warning——存证是硬需求，体检表是旁证，不能让一张坏图挡住入库。`pillow==13.0.0` 钉进 `requirements.txt`（此前只是传递依赖）。列表里 `original_filename` 为 NULL 时显示 `file_path` 的文件名，不再出现「未命名文件」这种零信息占位。
- **凭证两列：图像元数据与识别快照**（evidence-naming，票 01，ADR-0022／ADR-0023）：`screenshot_evidence` 加 `image_meta`（原图白名单 JSON：`width/height/bytes/format` + EXIF 子集，**不存 GPS**）与 `recognition_json`（识别快照，`recognized` / `adopted` 两段对照，供日后识别率检查）。迁移 `0032` 只 `ADD COLUMN`、不做表重建，逐列判定幂等（二跑 `noop`）；顺带把 23 条「临时文件名被当成原始名」的脏数据置 NULL——真名 5 条不动，**一个文件都没搬**。列表接口只吐 `image_meta` 与识别**摘要**（哪一次、什么引擎、识别几行、采纳几行、块有没有被丢），不吐可达 1 MiB 的快照本体。
- **解析规则体系 P1：词表落库 + 教学规则拆表 + 写入口收口**（parser-rules，票 01）：OCR 解析的七组出厂词表（金额列头 / 按钮黑名单 / 汇总标签 / 名称排除 / 收益区 / 闭合符 / 噪声正则）从硬编码搬进 `services/parser_rules.py`，并新增 `parser_vocabularies` 表存**增量覆盖**——生效词表 = 出厂 ∪ add − remove，出厂值永远留在代码里，`reset` 即删行回退，因此「改坏一次波及所有来源组」不会变成不可恢复的事故。教学规则（`rename` / `ignore` / `exclude`）从 `source_groups.rules` JSON 拆进独立表 `source_group_learned_rules`（带来源、时间、排序与命中计数列）。写入口统一收口：未知键与坏 JSON 由「静默保留 / 静默降级为无规则」改为 422 `SOURCE_GROUP_RULES_INVALID` 并回带键名——此前一个写坏的 JSON 会让该来源组的全部教学规则静默失效，脏资产开始进账本而界面无任何异常。解析引擎改为消费词表对象，算法结构一行未动：16 组几何 fixture × 11 组规则组合共 187 组、另加 59 条专项断言，与改动前实现逐项相等。
- **分类法重构：记录级 EAV → 资产项级列 + 受控字典**（ADR-0019，票 01）：资产类别（树状，`side_scope` 约束资产/负债/通用叶子）、机构、渠道、成员、标签拆为五张独立字典；资产项承载大类/机构/渠道/归属/流动性天数/风险等级六个单值维与一张多值标签绑定表，分类变更以 append-only 史表留痕。旧 `attributes` / `asset_attributes` 两表下线，`/api/attributes*`、`/api/dimensions/*`、`/api/management/{fund-types,accounts}*` 与三个 analysis 维度端点统一 410 `EAV_ATTRIBUTES_REMOVED`；新 `/api/classification/*` 30 个端点接管读写。迁移 `0026` / `0027`，规模明细见本地 spec（不入库）。
- **草稿态（draft）与未分类披露**（ADR-0020 D1，票 01）：新建资产项一律 `draft`，四个必需维补齐后自动转 `active`；草稿项**照常计入净值**——钱是真实存在的——但 `GET /api/analysis/summary` 必须同时给出 `unclassified_count` / `unclassified_asset_amount` / `unclassified_liability_amount`。「口径包含它」与「界面装作它不存在」不能同时成立，故披露与计入绑定。前端新增 `DraftClassificationBanner` 与汇总页披露位。
- **总额同源对账契约**（ADR-0020 D4，票 05）：`test_total_consistency.py` 登记 10 个已声明总额出口，与 `get_summary`（最新快照）/ `_sum_as_of`（历史 as-of）两个基线**精确相等**；披露只做单点（不在 20 个出口各复制三个字段），靠不变量守一致性。
- **空库重建管道与部署工件**（票 04）：`scripts/rebuild/*` 七步（分类提案／改名提案／只读导出／清洗计划／空库重建／不变量验收／合并边界）、`scripts/archive_item.py`、`asof_diff_report.py --baseline-db` 双库 as-of 逐日 diff；`deploy/` 增 compose、Dockerfile×2、`.env.example` 与 backup/init/package/smoke 四脚本，覆盖切库 runbook 全链。
- **写入路径同值 skip（P4）**（票 04）：按账期批量录入、复制上一条、CSV 导入、截图采纳四条路径统一「同 `(amount, side)` 不建行、不写审计」，与 `set_current_value` 的 `unchanged` 先例对齐；重复录入不再制造同值行，`skipped_unchanged` 逐层透出。
- **资产当前值（就地编辑）**——改一个资产的金额不再需要「找到那一行 → 打开弹窗 → 走更正」。`/records` 增「当前值 / 记录明细」双视图，**默认当前值**（URL 记 `?view=current|ledger`）：每个资产项一行＝截至参考日的最新有效记录，金额就地可改、失焦即存，无弹窗无确认；每行必显估值日与距今（>30 天弱提示），默认金额降序、带「隐藏零值」开关；手机端在卡片上就地出数字键盘。
  - 后端：`GET /api/assets/current`（派生读模型，含 `freshness: fresh|stale|missing`、未录入项亦列出、`is_latest`、不分页）与 `PUT /api/assets/current/{item_id}`（唯一分支点 `set_current_value`：参考日已有有效行 → `correct` 更正、日期不动；否则 `auto` 建当日新行；`correct_only` 无行 → 409 `no_record_for_date`；金额同值 → `unchanged`，不写行不写审计）。幂等复用既有 `Idempotency-Key` 请求头，未新增第二套机制。
  - 撤销：新增 → 删除该行；更正 → 更正回原值；均复用既有端点并写审计，不新增后端撤销接口。
  - 语义边界：只改交互，不改账本存储；历史仍不可改写，ledger 视图与 `CopyFromLastPanel` 批量重估路径均未改动。
- **既有资产项之间补登记关联（改关联）**：历史切换多为「新建项 + 归零」落地，承接方早已存在，而原血缘接口要求目标为新项——现在可以 `POST /api/lineage-links/attach` 在既有项之间登记（1→1 改名/更正、N→1 合并、1→N 拆分），只改归属口径、**不迁移历史行**，前身项自生效日起终止计量。
- **身份族读数**：分项趋势可按 `item_id` 取「身份族」序列（前身/后继按日取最近值求和，切换日不双计），分析页卡片可切换「含前身后继 / 仅本项」并给出当日构成；资产页身份面板显示前身/后继族树。
- **一对多血缘拆分**：汇总项拆成多个明细项现在可以登记（`POST /api/lineage-links/split`）——一次事务建子项、写 split 链与口径承接、最后按生效日归档父项；比例合计须为 100%，此前 1→N 无法表达（只能补 0 值，父子关系丢失）。
- **导入身份裁决**：截图采纳时若某行名字匹配不到任何既有资产项、而同账户已有同机构资产项，该行会被标为「疑似改名/拆分」，必须先三选一（沿用旧项（合并）／作为新资产新增／撤销该行）才能采纳；选择合并则复用血缘登记（按声明的生效日归档前身项 + 口径承接 + 审计），并另留一条人工裁决审计。未表态时后端返回 400 `identity_decision_required` 且零写入。
- **资产项身份漂移诊断**：净资产趋势的每个点新增 `added` / `vanished` / `drift_suspect` / `drift_amount`（阈值 10%，模块级常量），用于回答「这一刻是不是有同一笔资产换了身份」。前端在可疑点画空心菱形角标，tooltip 与页内面板列明细（金额降序、零值剔除、Top-5），文案只陈述观测不做归因。
- **截图凭证管理页与读接口**（evidence-manager，票 02／票 04）：凭证此前**只能存不能看**——29 条原图躺在盘上，没有任何界面能翻、能看、能删，只有一个按 id 取单条的端点。新增 `GET /api/screenshots/evidence`（来源组／日期区间／使用状态／关键字筛选 + 分页，按 id 倒序）与 `GET /api/screenshots/evidence/{id}/image`（`FileResponse` + `Cache-Control: private, max-age=86400`，`?download=1` 转附件，中文名按 RFC 5987 编码），前端 `/data/evidence` 页给缩略图列表（原图直出 + `loading="lazy"`，不生成缩略图）、详情弹层大图与元数据、「关联账本行 N 条」下钻。**「已使用」只定义一处**：`evidence_service.used_expr()` 把资产行／审计／本金变更／转账事件四类引用或起来，列表徽章、删除守卫、批量候选共用同一谓词——同一概念在三个界面各写一遍是这类功能最常见的腐烂方式。取图路径做根目录 containment 校验（`realpath` 必须落在凭证根内，否则 400 `EVIDENCE_PATH_INVALID`），`../../etc/passwd` 与符号链接逃逸都有测试钉住。
- **凭证删除与批量清理**（evidence-manager，票 03）：单条删除三态——未使用 → 删行 + 审计 + 删文件（`204` 无 body），已使用 → `409 EVIDENCE_IN_USE` 附四处引用计数（**不提供 force 后门**，证据链底线），不存在 → `404`。顺序固定「先删行、后删文件」：反过来会出现「行还在、文件没了」的假证据，而「行没了、文件还在」只浪费一块磁盘。删除审计的 `evidence_ref` 必须留空——审计若仍指向即将消失的凭证就是悬空外键。批量清理 `POST /api/screenshots/evidence/purge-unused` 只挑未使用，`dry_run` **默认 true**（先出清单与总字节，确认后才真删），每条独立事务、单条失败进 `failed[]` 不拖累其余，`limit` 防一次删穿。
- **按凭证反查账本行**（evidence-manager，票 05）：`GET /api/assets` 新增 `source_evidence_id` 筛选（与既有的同名**写入**参数职责不同，代码里加注释隔开），`/records?evidence_id=N` 显示「仅显示凭证 #N 关联的行」提示条并能一键清除（只摘该参数、保留其余筛选）。凭证详情「关联账本行 N 条」由此可下钻。**条数按全行算**（含被更正的 superseded 历史行）：若只算有效行，删掉「仅被历史行引用」的凭证会让历史行的 `source_evidence_id` 悬空。

### 变更
- **图内数据表跟随时间窗口，折叠表改成惰性挂载**（chart-table-window-perf，票 01/02，L1）：上一轮把两张冗余「数据明细」卡折进图内折叠表后，表仍按旧契约「始终全量账期」渲染——而这条契约的原始主体是**页面级**明细表（已随 `analysis-detail-prune` 删除），图内表把它的口径继承了下来。结果是在 day 粒度 730 期下，图为「近 24 期」而表照旧铺 730 行，图表与其 canvas 等价物不对等。现在表与图同源：行取 `categories.slice(start)`，追加的「较上期变化额 / 较上期变化%」按同一窗口切片，caption 随窗口写「（近 N 期）」，只有窗口覆盖全部账期时才写「（全部账期）」——全量读法仍由「全部」档一键可达，那不是消失而是归位。同时折叠表改为**惰性挂载**：`<details>` 首次打开前不渲染 `tbody` 行（`<details>` 仍非受控，`onToggle` 与命令式 `openTable()` 双路径都置位，首次打开后保持挂载不回落）。730 期实测：折叠态 DOM 节点 10223 → **544**，页面高度 2138px 与改前 Δ=0（可见行 0，表体不进布局），展开后仍可读全 730 行（`max-h-64` 自滚，页面只增 260px）。四粒度回归 month 24 / quarter 8 / year 2 / week 104 与图首尾一致，390px 无横向溢出。
- **备份保留策略改由后端调度器执行**（audit-reason-enforce）：此前由前端页面打开时循环调 `DELETE /api/backup/delete/...` 清理超龄备份，既依赖有人打开页面，也让「清理」伪装成人的删除动作（要理由没法给）。现在清理在服务端按策略执行并留 `system` 留痕，页面只负责展示与保存策略文案。
- **趋势图的点击命中面扩到整个绘图区**（chart-click-affordance，票 01，spec 待冻结）：原先只有 4–5px 的折线点/柱体本身能触发「点某月看该月漂移明细」，绘图区空白处点了没反应——而 tooltip 末行恰好写着「点击查看」，业主的实际反应就是去点浮层（浮层跟随指针、`enterable:false`，物理上点不到）。现在按指针所在**账期**命中：`containPixel({gridIndex:0})` 把标题/图例/轴标签/卡片留白挡在外面，`convertFromPixel({xAxisIndex:0})` 取最近列并夹紧首尾半档，`index = start + 列序号` 仍遵守窗口切片；grid 内光标变手型。同时**移除原先绑在系列上的 click**，只保留 zrender 单通道——两条通道并存时同一击会「展开后立刻收起」。`onDatumClick` 契约、分析页消费逻辑、首页趋势图（不传该 prop，行为零变化）均未改。
- **恢复备份默认会拒绝「把库换回旧版」**（audit-best-practice，票 03，ADR-0028）：`POST /api/backup/restore` 在替换数据库之前先比链头——目标包比当前库旧（`db_older`）或行数相同而链头不同（`diverged`）时返回 409 `AUDIT_ROLLBACK_DETECTED`，要越过须带 `confirm_rollback=true`（表单字段，默认 false），越过事实会落进恢复留痕。旧裸 `.db` 与 `POST /upload-db` 无 manifest 锚，判定 `unknown` 放行（这类文件没有可比对的链头），两类路径的返回体与证据合并行为不变。被拒绝的恢复不产生 rollback bundle、不动当前库。
- **识别快照的引擎标签改为「中文 + 括号原值」**（evidence-recognition-view，票 01）：`ai` → 「AI 识别（ai）」、`ocr` → 「OCR 识别（ocr）」，无值仍是「—」。快照节原先直接渲染 `summary.engine` 原值，与列表列的 `engineLabel` 不一致；保留原值是便于与接口字段对照。
- **凭证查重收窄到「同一来源组」**（evidence-dedup-index，票 01，ADR-0024）：同一张图上传到**另一个**来源组时，不再静默复用第一个组的凭证行——旧行为会让 `source_group_id` 与文件名里的 `sg<id>` 双双说谎（图明明来自 B 组，却记成 A 组的凭证）。现在跨组各存一份文件、各带自己的 `sg<id>`；同组同图二次上传仍复用同一行，且复用时会话属性（`data_date`/`supplement_rows`/`engine`）随最近一次采纳富化（与生产 `evidence_id` 分支同口径）。命令行 `screenshot_cli.py --apply` 走的 legacy 分支也统一成这条口径（同图同组二次 apply 返回既有 `evidence_id`、不新建行），不再是「每次 apply 一条独立凭证行」。
- **凭证列表的图与文件名可直接点开详情**（evidence-detail-entry，票 01）：缩略图与原始文件名两列原本是纯展示，「查看」藏在最右的操作列——而用户最想点的恰恰是图和名字。现在两者都是 `<button>`（`aria-label` + `focus-visible` 焦点环 + hover 反馈），直通同一个详情弹层，不新增页面也不新增接口；**原图丢失的 404 占位态同样可点**（此时更需要进详情确认磁盘状态）。不做整行点击：会和勾选、横向滚动抢事件。
- **凭证下钻默认展开更正历史**（records-evidence-history，票 01）：从凭证详情点「关联账本行 N 条」进入 `/records?evidence_id=N&view=ledger` 时，「显示历史（含被更正）」自动打开——下钻的唯一目的就是「看这张凭证产出的全部行」，而默认藏起更正历史会让条数少于凭证页显示的 `used_rows`（全行口径），用户会当成丢数据。生效值 = 用户手动覆盖 ?? URL 带 `evidence_id`，故手动取消随时有效；覆盖值只存 state 不写 URL（深链语义保持简单，刷新丢失是已知代价）；清除 `evidence_id` 不复位。运行库 4 张凭证有此差距，最大一张从 12 行补到 13 行。
- **来源组删除守卫改为「只被已使用凭证阻塞」**（evidence-manager，票 03）：旧逻辑是级联改名留下的残影——组里只要有凭证（哪怕全是从未采纳的废图）就拒绝删除，且是不带机器码的 400。现在：有已使用凭证 → `409 SOURCE_GROUP_HAS_USED_EVIDENCE`；只有未使用凭证 → `409 SOURCE_GROUP_HAS_UNUSED_EVIDENCE`（附数量，提示可去凭证页清理），要连废图一起删必须显式 `?purge_unused=true`——**绝不静默级联**。
- **凭证来源组从名字快照改为外键**（票 01）：`screenshot_evidence.source_group` 存的是来源组 slug 字符串且**没有外键**，组改名靠路由里一段手写级联去逐条改历史凭证——漏一次就留悬空；而这段级联又让「删组前检查是否被引用」永远为假（检查拿的是刚被改过的字符串），等于两个缺陷互为掩护。改为 `source_group_id` 外键（迁移 `0029`：12 步重建、悬空 >0 即 fail-fast 中止、重建后校验外键与完整性），读路径 join 取当前 slug——**对外 API 契约不变、前端零改动**；组改名从此不需要任何级联代码，删组守卫改按 id 判定。写入侧收敛为单一解析入口（slug 或 label 皆可，未知组 400 `SOURCE_GROUP_UNKNOWN`）。重建管道同步随迁：`s1` 导出 `source_groups`、`s4` 按 slug→id 映射并在映射不全时中止、`s4b` 的悬空校验兼容新旧两种结构。**运行库执行留给停服窗口**（副本库演练已通过）。
- **归属读列**（ADR-0020 D3，取代 ADR-0011 决策 1，票 02）：`asset_items.owner_id` 成为当前归属的唯一真相源，`asset_item_owner_changes` 降为纯审计流水。新增迁移 `0028_owner_column_backfill` 按每项最新合法史行回填列（`family_pool` 保持空、不伪造史行）；归属变更史与列**同事务**写入，分类编辑入口此前只写分类史不写归属史，该路径的审计链现在是接上的。
- **确定性门十项 → 九项**（ADR-0020 D2，票 03）：删除 `GATE_DATA_PARTIAL`，顾问确定性门变为 8 blocking + 1 warning；`data_coverage` 指标整块退场，「部分录入」标记改判为「沿用天数」（`carried_items` / `carried_max_days`）。
- **首页「未录入」催办退役**（ADR-0018）：横幅的判据是「参考日＝全库最新数据日期，该账户在参考日无任一条资产行即未录入」，于是「今天只录了一个账户」＝参考日跳到今天＝其余账户全变未录入，一次合法的小步更新制造一次全网假警报；粒度也与当前值模型错位（账户 vs 资产项）。处置：删除首页横幅与 `GET /api/management/entry-status`，删除账期补录三件套（`POST /accounts/{id}/zero-fill`、`GET /accounts/{id}/fill-preview`、`account_reminder_service` 的 fill/preview 机器）——「无变化」不再写一条新快照，「清零」只作测量值而非占位。新鲜度统一由记录页「当前值」视图的 `fresh / stale / missing` 承担，首页不再设第二通道。覆盖度（`_coverage_for`）保留为**内部描述性指标**，5 个消费方（summary / 趋势部分录入 / `data_coverage` / 目标达成度 / 顾问 DATA_PARTIAL 闸门）零变化。记录页「批量按账期添加」与「复制上一条」是显式编辑能力，保留。已知能力净损失：账户级「从未录入」总览消失（一个资产项都没有的账户不再出现在任何视图）。
- **as-of 聚合口径收敛**：快照日期列表过滤被更正行（superseded）；**归档资产项自归档生效日起**退出净值/占比/预警（ADR-0017，历史测量值保留、不回溯抹除）；`/analysis/mixed-chart` 的折线与柱状共用同一次聚合，不再各跑一遍；预警服务的多处读取、占比期间列表、记录页日期列表统一接入同一谓词，口径只在 `snapshot_identity` 定义一次。陈列类读模型（资产当前值）仍展示归档项的最后值——陈列不是汇总。
- **占比口径修正（改名双计）**：`proportion_service` 的最新值子查询改按**资产项身份**分组（`asset_item_id`，缺失时回退名字），与 `analysis_service` 的 as-of 口径对齐；此前按 `asset_name` 分组，改名后同一资产项会被拆成两个而双计（新用例在旧代码上红：2200 vs 1200）。行身份表达式收敛到 `backend/services/snapshot_identity.py` 单一定义处，分析／占比／当前值共用。

### 移除
- **两处分期录入入口退役**（asset-record-edit，票 10/12，ADR-0031）：「复制上期」（`POST /api/assets/copy-from-last`）与「批量按帐期添加」（`POST /api/assets/batch-create-by-period`）连同两个 service 函数、三个孤儿辅助函数、前端两整块面板与入口按钮、两个客户端方法、三个测试点、两条审计契约行一并删除，审计契约表 **133 → 132**。退役理由不是「没人用」而是**语义失效**：两入口把账期折成代表日（`period_service.get_representative_date`）再写进 `asset_date`，在「时点快照 + 更正链」模型下生产的是合成时点。`period_service.py` 整体保留（趋势/占比/预警仍用其分组参数意义），存量历史行不清洗（`assets` 无来源列，无法按来源区分）。
- **截图凭证的对账锚点字段 `anchor_amount` 删除**（evidence-manager，票 01，ADR-0021）：该字段本意是与系统合计比对，实际从未通用化——提取逻辑硬编码单一 App 文案，全仓没有任何比对/报警/导出读它，运行库 29 条凭证里只有 1 条有值；更根本的理由是差额不可解释：截图总额常含「预计可借」「昨日收益」「已赎回仍展示」等有意排除项，「合计 − 锚点」的差无法区分「解析漏读」与「主动剔除」，做不成报警条件（分不清对错的哨兵只会被关掉）。迁移 `0030` 12 步重建删列（重建后 9 列，索引与外键、`ck_evidence_engine` 原样保留），模型／两个 schema／路由 4 处／服务 6 处／两个 CLI 脚本／离线重建管道 `s2`+`s4` 的导出与落库列／前端 3 个类型 + 2 处展示（含批次合计 `anchorTotal`）全部清理。**运行库执行留给停服窗口**（副本库演练：行数 29 不变、列集合与索引逐字核对、`foreign_key_check` 空、`integrity_check=ok`、二跑 `noop`）。
- **数据分析页的两张「数据明细」长表退场，变化两列折进图内数据表**（analysis-detail-prune，票 01/02，L1）：趋势 tab 与占比 tab 各有一张独立明细卡，而图卡自带的「数据表」折叠本就是同一份账期明细（`ChartFrame` 的三图族共用，永远全量账期）——于是同页并存两份全量副本，趋势那张还移动端列表＋桌面表格同时常驻 DOM（行数 ×2），日粒度动辄数百上千期。现在两张卡删除：占比 tab 的信息由图内表「名称/金额/占比/备注」承接；趋势 tab 原本独有「变化额 / 变化%」两列，由 `TrendChart` 新增可选 prop `extraTableColumns` 追加进图内表（列位置在系列列之后、口径列之前，`values` 按全量账期下标对齐，缺位落「—」）——命名避开既有柱系列「变化额」，表头为「较上期变化额」「较上期变化%」，格式化复用 `DirectionText` 的 `formatDelta`，与旧卡单元格逐字相同。实测：同一数据集 16 个账期逐格比对零差异，页面级表格由 1 张 16 行降到 0 张（唯一数据表处于折叠态、可见行数 0），390px 无横向溢出。「数据表始终全量、窗口只切图」的契约未动。

### 修复
- **录入侧把归档项当「在用」：搜索栏搜不到它，「未识别项」却列着它，钱写进去在合计里消失**（archived-ingest-scope，票 01/02，ADR-0032）：截图复核页有两条互相打架的规则——未识别项面板与匹配候选按 `assets` 最新快照聚合、完全不看资产项状态，归档项因此照常出现；关联选择器却只列 `active` 并会连带漏掉 `draft`（后端只拒 `archived`）。更严重的是写路径：OCR 再读到已归档的名字会精确命中归档项并生成 update 行，金额写进去后 as-of 聚合自归档生效日起剔除该项，用户看到「已录入」但总额不变。现在「在用」判据统一成 `status != 'archived'`（`active` 与 `draft`），并收口在读法的**唯一入口** `_group_latest_rows`——未识别项、候选匹配、补录一条下游全部随之一致；命中已归档名时预览改为 create 行并附「历史上已并入 X（该名 YYYY-MM-DD 起归档）」提示，写路径 `_upsert_asset` 在落任何 `assets` 行前对归档项回行级 409 `ITEM_ARCHIVED`（create/update/zero 三条路径共用），分类补全也不再把它静默吞掉。前端关联弹窗同步：可选中=非归档（`draft` 回归），搜到归档项时单列一段**不可点选**的说明行并指出该并入的在用项，理解成「新增就好」的旧文案一并改掉。口径要记牢：归档项**不进**匹配候选、不进未识别面板、不被补录命中、不可写入；`total_current_system` 这类锚点自本次起不含归档项（差额等于归档项现值之和）。无迁移、无新端点；新增 `backend/tests/test_archived_ingest_scope.py`（12 例，旧实现上 9 红）与可复跑验收脚本 `scripts/verify_archived_ingest_scope.py`（在 dev 库**副本**上直驱 preview/apply，五步 PASS/FAIL，输出脱敏，`sha256` 前后一致证明现役库零写入）。
- **告警的周期选择器在直接进入页面时报错**（asset-record-edit 副产品）：`frontend/src/app/alerts/components/PeriodPicker.tsx` 用 `date.week()` 却从未注册 `weekOfYear` 插件，此前靠记录页模块作用域的 `dayjs.extend` 顺带生效；记录页移除分期编辑代码后失效，`next build` 报 `Property 'week' does not exist`。现在组件自己注册插件，直接打开 `/alerts` 选「周」周期不再抛 `week is not a function`。
- **趋势图 tooltip 的「点击查看」改成「点击查看本期明细」**（chart-click-affordance，L0）：原句主语缺失——tooltip 跟随指针且 `enterable:false`，指针永远够不到它，业主的第一反应就是去点浮层本身；而真正能点的只有柱形本体或折线数据点（宽 2–5px），绘图区空白处点击不产生事件。本单先把对象说清（L0，行为零变化）；「命中面扩大到整个绘图区」属 L1，规格见 `.scratch/chart-click-affordance/spec.md`（待冻结）。
- **删不存在的备份文件不再报 500**（backup-delete-404）：`DELETE /api/backup/delete/{filename}` 内层抛的 404 被外层 `except Exception` 包成了「删除失败 500」，客户端会误判成服务端故障。现在 `HTTPException`/`ApiError` 原样上抛：文件不存在就是 404（且零审计行，未删即无留痕）；真正删文件失败仍是 500。`/api/imports/backups/{filename}` 同类端点核查后无此问题。
- **截图版式误判为多列，一张 9 项的对账单只识别出 1 项**（multicolumn-signature，票 01）：示例银行账户总览截图（9 个资产项、合计 1,000,000.00）走 OCR 管线只吐出「示例活钱A ¥10,000.00」一项，其余 8 项全靠「转用 AI 识别」兜底补录。根因在**版式判定**而非 OCR 质量：该图把「示例活钱A」这一行 OCR 出了两份（`"10,000.00 >"` 与 `"10,000.00>"`，仅差尾部空格），而 multi_column 签名用**原始文本**判「同行金额互异」——`strip()` 后两者仍不等 → 被判成两个不同的金额列 → 整图判定为多列；`_organize_multicolumn` 要求每行 ≥2 个金额，全图只有那一行满足，于是其余行全部丢弃。两处收口：① 金额互异改按 `_cell_amount` 解析后的 **Decimal** 比较，OCR 复制行与单位后缀差异不再伪造出「两列」；② 多列签名需 **≥2 行支持**——真三列表每行都多列，单行命中不足以绑架整图，否则继续往下判纵向/卡片/横排。修复后同一张图自动判为 horizontal，识别 10 项、合计 1,000,000.00，与图中「总资产」块逐分相等。既有两个 `duplicated_row` fixture 用的是**两份完全相同的文本**（strip 后本就相等），因此一直测不出这个真实变体；新增 `backend/tests/test_layout_signature.py` 15 例（T1–T7 版式真值表 + E1–E8 排除边界），同一套用例在旧实现上 **6 红 9 绿**。另：`exclude` 从**全名精确匹配**改为「精确相等 或 前缀后紧跟非字母数字」（判据 `str.isalnum()`，CJK 属 alnum），`-示例数字钱包` 从此命中 `-示例数字钱包-尾号0000`，而 `-存款` 不误伤 `-存款证明`、`-沪深300` 不误伤 `-沪深300ETF`；不引入任何字面量字符集常量。既有 18 条示例银行资产逐条对照，新旧口径仅 1 条差异，正是上述预期收益。**零数据变更**：不写任何教学规则（`ignore` 是子串匹配，`余额`/`示例数字钱包` 一旦落库会静默吞掉「示例活钱B余额」这类真实行），`示例数字钱包-尾号0000` 与 `余额` 两条 0.00 行仍按既有口径进清单（update / diff 0）；`_organize_*` 与 `parser-rules-refactor` 的行为等价矩阵一行未动，全量 `backend/tests/` 1584 passed / 1 skipped。
- **批量清理预览把「实际删除数」当「候选数」，显示 0 条且确认按钮禁用**（evidence-manager，票 06）：`purge_unused` 在 `dry_run` 阶段返回的 `deleted` 语义是「本次实际删除了几条」，故恒为 0，候选数在 `items` / `total` 里。前端把 `deleted` 同时用作预览计数、空态判断和确认按钮门控，结果是选「示例银行A」清理时显示「将永久删除 **0** 条…释放 128.50 KB」「范围内没有可清理的未使用凭证」，而该组实际有 3 张未使用凭证——**按钮永久禁用，批量清理实际不可用**，且「0 条却释放 128 KB」自相矛盾（`freed_bytes` 走的是 `items` 求和）。修法：预览计数与门控一律改用 `items.length`（本次真正会删的条数），`deleted` 只留在「已删除」结果区；`truncated` 时明写「候选共 N 条，本次只处理前 200 条」；预览新增可展开的候选清单（`#id` + 路径 + 字节）落实 spec D5 的「出示清单」，此前只给了数量。`EvidencePurgeResult` 同步补齐 `items` / `total` / `truncated` / `limit` 四个字段并在类型注释里写明两个计数的区别。后端零改动。
- **重建丢掉凭证组配置**（票 01）：重建 spec 的 D8 把「凭证组」当纯配置跳过，实际执行后旧库 5 个来源组只剩 seed 的 1 个，`source_group_accounts` 5 条映射剩 1 条，29 条历史凭证中 20 条的 `source_group` 指向已不存在的组；一并丢失的是每个组的 OCR 识别规则 JSON（`rename`/`ignore`/`exclude`/`layout`/`amount_header`，界面没有编辑入口，无法重建）。处置：`source_groups` 改判为随迁事实，新增 `scripts/rebuild/s4b_restore_config.py` 从旧库按 slug 对齐恢复（只补空字段、不覆盖人工改过的值、机构名精确匹配、默认 dry-run），在运行库上执行并复核 5 组 / 4 组带规则 / 5 条映射 / 凭证悬空归零；D8 加修订段、§7 runbook 增一步，s1 的「不导出」注释同步更正。新增 `backend/tests/test_restore_source_groups.py`（5 例：dry-run 零写入、apply 计数、幂等、不覆盖非空、机构未匹配不建字典）。
- **归因覆盖率把已归档项算作「在场未申报」**（票 01）：`principal_set_ratio` 的在场项集合走 `_active_rows`，该函数只看有效行、不看资产项状态，于是归档项未登记本金口径时被计入分母——ratio 偏低，`uncovered_items` 还点名一个已经退场的项（净值与四段恒等式不受影响）。修法：覆盖率循环叠加 `measuring_at`，与 `_net_at` 用同一谓词。
- **预警「单档合计」被放大成总资产**（票 01）：`_sum_at` 用**原始**维度字符串判断是否 join，而条件构造内部会归一化——存量规则里写作 `risk` 的（`risk_level` 的合法同义词）条件落在 `asset_items.risk_level` 上、join 却没加，SQL 退化成 `FROM assets, asset_items` 交叉积，只要该档位有项命中，算出的是全部资产之和（实测 100 应为 100、实际 600）。修法：`_sum_at` 入口先归一化维度；测试从「0 == 0 空过」改为设置真实风险等级并断言主名/同义词同值。
- **归因期末净值含已归档项**（票 05）：`attribution._net_at` 的取数只过滤更正行、不看资产项状态，归档项仍在场，与 `summary` / `proportion` 不同源却自称同口径；现叠加归档生效日判据。
- **配置分析分母不是 as-of**（票 05）：`get_total_assets` 用「当日有行之和」，当日无行的沿用项整支漏掉，且缺更正行过滤——现直接委托 `_sum_as_of`；`_amounts_by_item_column` 同改。
- **导出丢草稿项**（票 05）：CSV 显式排除 `draft`，导致导出与首页合计对不上账；移除该过滤后导出行与合计同源。
- **多图批次的「未识别项」误报**：同一来源组上传多张截图时，「系统有、本张截图无」的列表按**单图**口径聚合，把别张图刚识别到的资产也列了进来（实测 2 张图各命中 4 项，清单却列 10 条），用户一选「清0」就会误清别张图覆盖的值。修法：批次级并集反向过滤——只有「本批同来源组所有截图都没命中」的资产才算未识别，同一 `(来源组, 资产全名)` 只留一行（批次里第一张图为 owner）；同时「全部清0」在同组多图时不再下发 `unrecognized='all'`（后端清的是本图未命中的组内全部资产，多图各清一遍，结果取决于应用顺序），改按 owner 图下发 `zero_names`，同组单图载荷逐字段不变。新增纯函数 `frontend/src/lib/screenshotBatch.ts` 与 `node:test` 回归（`node --test src/lib/screenshotBatch.test.ts`，5 例）。
- **净值趋势单日假尖峰**：同一笔资产换用新资产项（改名/拆分的常见落地方式）后，遗留项没有当日 0 值快照，as-of 沿用把旧值带入新项首现的同一日，形成「单日虚增、次期立刻回落」的尖峰并污染最高值/最低值与环比。根因**不是**身份分组口径（同 item 改名已由稳定身份修好），而是「换了新 item」这条路径没有归零也没有血缘登记。处置：为遗留项补记 0 值快照（append-only，审计带 `request_id`）；机制侧把该形态做成可观测信号（见「新增」），并把「新项首次出现需人裁决」列为下期项。

### 维护
- **`mixed-chart` 的 day 粒度从「一期一条 SQL」改成「一次取数 + 内存扫掠」**（chart-table-window-perf，票 03，L1）：730 期原本要发 731 条 as-of 查询（每条都要 `max(asset_date) GROUP BY identity` 扫一遍 active 行），单请求 5.47s，是「切到日粒度卡 5.9s」的真凶。新增 `_latest_records_by_as_of`：一条 `assets LEFT JOIN asset_items` 取 active 行全集，按 `row_identity()` 身份分组后用 `bisect_right` 扫到每个 as_of 的最近日期、同身份同日整块返回（与 SQL 联接出多行的行为一致）；SQL 条数与账期数解耦，恒定 3 条。口径没有新规：`superseded_by IS NULL` 逐字照搬，归档排除只调 `snapshot_identity.measuring_at`（生效日**当天**即退出、NULL 剔除）这一个 Python 镜像，不手写第二份判断。等价性用旧实现副本逐点逐字段对账：真库 67 点与合成 730 期、day/week/month/quarter/year 五档**差异行数均为 0**；730 期中位耗时 149ms（旧 7064ms，47×），30/90/730 三档 117/122/149ms（比值 1.27，不再随账期线性），端点实测 0.63–0.73s。极限情形如实记着：133 活跃项 × 730 日（97k 行）约 2.4s，已登 `TODO.md`。
- **分析页验收脚本对齐新行为**（chart-table-window-perf，票 04）：探针原本在折叠态等 `tbody tr` 出现，惰性挂载后这一等必然超时并被 `.catch` 静默吞掉——每次白等 3 分钟还当作通过。现在改为「等接口响应 → 等 `data-state=ready` → 折叠态先断言行数 0 → 需要行数再点 summary 展开」，超时按 `FATAL` 退出而不是吞掉；断言按 spec A1–A8 重写且期望值全部由接口实测推导（不写死 730），趋势帧定位改按「含窗口 chip 组的 `figure[data-chart-frame]`」（页面上还有另外两族 `details`，不能取第一个），caption 改取 `table > caption`。两档参数化实跑各 38 项全通过，故意破坏可红且退出码 1。
- **分析页长列表验收可复现**（analysis-detail-prune，二轮补测，L1）：新增 `scripts/make_analysis_long_fixture.py` 与 `scripts/verify_analysis_ui.sh`（配 `scripts/analysis_ui_probe.js`）。前者用 sqlite backup API 取真实库一致快照、在副本上清空 `assets` 并灌入「N 项 × N 日」合成数据（合成值虚构，公开安全）；后者一条命令跑完造数 → 起后端副本 → 构建前端 → playwright 断言 → 清理（`--days N` / `--no-build` / `--keep`）。用 730 期实测补齐了此前只能间接论证的长列表结论：折叠数据表 730 行全在 DOM（+9.1k 节点、堆 12.9→20.4MB）但**可见行 0**，摘掉表体页面只矮 44px（＝summary 高度）→ 行数不参与布局；展开一次 reflow 105ms，390px 无横向溢出，11 项断言全通过。**顺带发现并登记 `TODO.md`**：切「日」到渲染完成 5.9s，几乎全是后端 `mixed-chart?period_type=day` 的 730 次 as-of N+1（单请求 5.5s），属票外后端议题。
- **解析规则合并收口到单一出口**（spec D9）：`get_group_config()` 的 `rules` 键此前只解析 `source_groups.rules` 列，0031 拆表后任何调用方拿它喂解析都会**静默丢掉表内教学规则**；`_serialize_rules()`（展示）与 `rules_for_parsing()`（解析）也各写了一遍「列 + 表」合并。现合并下沉为唯一实现 `_merged_rules(db, sg, *, strict)`：严格模式坏 JSON 抛 422（解析侧），宽容模式原样回显列（展示侧，不掩盖脏数据），两者只差错误策略、不差口径。`source_group_rules_service` 已反向 import `screenshot_service`，故 `get_group_config` 用函数内延迟 import 破环（同 `classification_service` → `asset_owner_service` 先例）。
- **配置分析一次请求只取一次 as-of 记录**：`get_total_assets` 与 `_amounts_by_item_column` 原来各自调用 `_get_latest_records_as_of`，同一份记录取两遍；现由 `_as_of_records()` 统一取数、两者经可选 `records=` 复用。合计仍由 `_aggregate_records` 产出（唯一定义），口径零变化；副作用是消除两次取数之间可能读到不同快照的缝隙。
- 迁移 `0026`（分类重构）/ `0027`（删 EAV）/ `0028`（归属列回填），`models.py` 同步 CHECK 与外键指向；旧迁移 `0008` / `0024` 补可空库重跑守卫。
- 迁移 `0031`（教学规则拆表 + 建词表表）：副本演练 `inserted=24 rewritten=4 nulled=3`、二跑 `noop`、`foreign_key_check` 空、`integrity_check=ok`；`models.py` 追加 `ParserVocabulary` / `SourceGroupLearnedRule`。**运行库待停服执行**（执行后 `schema_version` 应为 30 行）。
- `check-public-safety.sh` 补 `safe.directory` 包装（票 05）：仓库属主非当前用户时原脚本在 `rev-parse` 拿到空串后 `cd` 到 null 直接崩，这道理线实际不设防；非属主身份裸跑现已通过。
- `.gitignore` 增 `.scratch/db-rebuild/`：重建方案含真实机构归口与真净额差额，永不入库（同 `.scratch/db-audit/` 先例）；决策的公开形态留在 ADR-0019/0020 与 `docs/design/资产分类规范.md`。
- **数据库审计（只读）**：40 张表 / 22 个迁移 / 31 个触发器 / 20 个 FK 列索引缺口全量核对；报告见本地 `.scratch/db-audit/audit-2026-10-29.md`（不入库）。
- **P0 零损失清理**：`export_history` 删除 145 条指向已删文件的悬空行（181→36，保留 31 条有文件 + 5 条无路径的历史行）；`data/backup` 按「每天留最新 + 全局留最新 2 份」裁剪 15 个 bundle、释放 353.7MB，`rollback_*` 与裸 `.db` 未动；补跑并登记迁移 `0022`（此前库落后代码一个迁移，候选集为空、纯空跑）。三步均写 `audit_log`（`entity='maintenance'`，actor=system），审计链 `last_id=row_count` 自洽。
- 新增 `scripts/db_cleanup_p0.py`（默认 dry-run，`--apply` 才执行）：`export-history` / `dangling-targets` / `backups` 三个子命令，重复可用。
- 新增 `scripts/schema_drift_report.py`（只读）：把生产库与 `models.py` 逐表逐项比对，输出 BLOCKER/KNOWN 分类；当前 **0 BLOCKER / 43 KNOWN**（33 条迁移专属触发器 + 4 表列序 + 3 张非 ORM 表 + 2 处服务端默认 + 1 处可空性），退出码可直接接 CI。
- **迁移 0023（只做加法）**：补 19 个读写路径索引（`assets.superseded_by`、`audit_log.evidence_ref/request_id`、凭证反向追溯、支出/规划/建议域 FK）＋ `asset_item_names` 双 append-only 触发器（审计 D3/D6）；`models.py` 同步声明，create_all 路径与迁移库一致。
- 清理 1 条跨维度错挂的配置目标（`fund_type` 维度指向 `account` 节点、占比 0），审计行 + 阈值史留痕；`fund_type` 顶层与 `liquidity_rating` 目标本身未改（顶层合计与 `灵活取用` 父子差的目标值待业主设定）。
- **迁移 0024（值域约束）**：12 张表按最小 12 步重建补 19 条 CHECK（`assets.amount >= 0` 等），逐表断言行数/索引/触发器复原，重建后 `integrity_check=ok`、`foreign_key_check` 空；`models.py` 同步 19 条 `CheckConstraint`。

### 文档
- 新增 **ADR-0020：草稿态计入净值、确定性门九项、归属读列——三项追认 + 一条不变量**；ADR-0011 / ADR-0013 / ADR-0019 各加一行修订指向它，原文不改写（保留演化痕迹）。
- 新增 **ADR-0019** 与 `docs/design/资产分类规范.md`（七维分类法、命名规范、触发器约束）。
- `CONTEXT.md` 分类法整节重写（此前仍描述已被删除的 EAV 属性表），补「草稿态」「归属」词条，资产项 `status` 补 `draft`。
- 新增 `docs/WORKFLOW.md` 文档驱动开发规范（分级 L0–L3、五道门 G0–G4、提交锚点、AI 权责边界）+ `docs/templates/{spec,issue}.md` + `scripts/check-doc-driven.sh` 校验。
- 新增 **ADR-0018：未录入催办语义废止——覆盖度退为描述性指标**——参考日不再被录入行为反向定义，首页不再催办；覆盖度保留但只描述「历史某一期数据有多密」。同步修订 ADR-0009（③ 互补关系与 Banner 语义条款标注废止）、ADR-0015（补录 `mode=zero` 写路径标注为历史）、`CONTEXT.md`「覆盖度」词条。
- 新增 **ADR-0016：当前值就地编辑仍为追加**——就地改值不改写历史：参考日已有有效行则更正（`superseded_by` 链），否则新建当日行。
- 新增 **ADR-0017：归档资产项终止计量**——`archived` 自归档生效日起退出 as-of 聚合（历史测量保留）；「哪些行参与口径」与行身份同处定义于 `snapshot_identity`，分析与占比共用。
- 新增 **ADR-0015：零额快照合法化**——金额值域改为 `amount >= 0`，0 是合法测量值（语义：该快照日余额为零），负数仍禁止、方向由 `side` 表达；存储层 `CHECK` 留待 0023。
- 口径同步：`CONTEXT.md`（资产记录 + 本金口径 + 资产项身份 / 归档＝自生效日终止计量 / 沿用值不含归档项）、`business-architecture.md`（增补记录 v1.3 + R-1）、`system-architecture.md`（3 处）。
- `database-map.md` 升至 **v6.0.0**：24→40 张表、补齐 0015~0022、31 个触发器/15 张表、真实索引与不变量清单、8 条已知差异。
- `MIGRATION.md` 补 0011~0022 结构条目；`docs/design/README.md` 表数与日期对齐。

## [5.10.0] - 2026-10-29

### 新增
- **打磨期**（四期家庭域暂缓，先把单用户系统收口）：
  - 写侧正确性：CSV 导入归属改写 **item 级**（不再写记录级）；保留名「家庭池」三旁路统一（导入／attributes／asset_owners）；`/api/attributes` 删除 owner 引用检查；`_mark_primary` 仅 family；自动触发单轮上限 3＋审计；迁移 **`0022_owner_import_backfill`**（记录级→item 级回填，`NOT EXISTS` 幂等＋审计计数）
  - **批量修改历史归属**：`POST /api/asset-items/owners/batch`（ids 1..200、逐项独立事务、同值 skipped、归档 `ITEM_ARCHIVED`、不存在 `NOT_FOUND`、幂等键）＋记录页多选/结果摘要/失败重试
  - **资产标签**（零迁移）：`PUT /api/assets/{id}/tags`（≤5、`TAG_LIMIT_EXCEEDED`）、`POST /api/assets/tags/batch`、`GET /api/assets?tag_id=`、CSV `tags` 列；设置属性页 `type=tag` 字典、录入专用选择器（不新建）、批量打标、记录页标签筛选
  - **复制上期 v1**：`copy-from-last` 增 `asset_item_id/display_name/tag_ids/target_exists/target_record_id/item_status`；预览（可复制/已录/归档/缺失）、只读合计与差额、`POST /api/assets/batch` 支持 `asset_item_id`＋`skip`、失败项重试
- **资产原子化信息架构 v1.0 冻结**（`.scratch/asset-atomic/design.md`，D0–D3；明确不实施）＋D0 体检摘要（真实库只读副本：硬阻断/阶段阻断 0；分类漂移 75 项次、归属未标注 20 项待签收；归档/转移/血缘均为 0）

### 变更
- 表达与提示：分配页明细表增「阈值（黄/红）」列＋图注「图示阈值带取首个目标」；建议/复盘列表恢复行内降级文案（仍仅 1 次列表请求）；失配目标显示「目标已删除（#id）」并禁用采纳；C1 无快照显示「等待首个 ≥ 生效日的快照」

### 验收
- 后端 **815 passed**；`tsc` 0；`eslint src` 0/0；`tokens:check` 通过；`check-public-safety.sh` ✅
- Bundle：records 296 kB、expenses/export 298 kB（≤300 预算），其余不回归

### 文档
- `.scratch/polish/{design-d2,p1-report,p2-report,p3a-report,p3a-ui-report,p4a-report,p4b-report,p5-report,d0-report}.md`；`.scratch/asset-atomic/design.md` v1.0；票 `polish/issues/01–03`；四期家庭域草案未冻结留档（`ba61d6e`）

## [5.9.0] - 2026-10-29

### 新增
- **三期（目标与建议）收官**（ADR-0010~0014；迁移 0015~0021）：
  - **C1 目标与达成度**：`goals`/`goal_versions`（draft/active/archived 状态机、每 Goal 唯一 active、版本不可变、判据快照）；`/api/planning/goals` 11 端点；C1 三指标 `goal_progress`/`goal_gap`/`goal_required_annualized` 接入指标面与判灯；`_sum_as_of` 改按资产项身份聚合（修复改名后新旧名双计，W0-P0）。
  - **归属收口**：`asset_item_owner_changes` append-only（成员/家庭池、同日更正、变更史）、读 item 优先＋记录级回退、旧写入口翻译、导出按 item、owner 删除保护。
  - **C2 双阈值与域级**：`allocation_targets.alert_threshold_red/relative_threshold`（0017）；`band_service` 单点判据（绝对 pp/相对 % 取先触发、红空=2×黄、相对下限 10%）；C2 逐行判灯＋priority/recommendation；C1 所需年化单侧判灯；`/api/metrics/status` 加性 `domains/events/summary`（6 域 worst/judged/total、gray 阻断、异动上升沿 new/ongoing、零落表）。
  - **C3 建议闭环与审计链**：`audit_chain`（sha256.v1 逐行 prev_hash、单行 CAS 防分叉、日封存＋日志/备份 manifest 外锚、`/api/audit-log/chain/{head,verify}` 7 判定码）；`advisor_*` 7 表＋隔离约束（零账本 FK、AST 禁写端口）；信息集冻结（封闭键空间 ≤40＋payload_sha256）；10 项确定性门（8 blocking/2 warning）；LLM 仅措辞（数字占位符＋服务端插值、45s、重试 1 次、无供应商 409）；`/api/planning/advice` 9 端点＋链 2；自动触发默认关（冷却 7 天、去重、启动续跑）、建议 30 天过期；采纳≠落账、执行备注独立。
  - **C4 复盘定格**：`allocation_target_revisions`（0020 append-only 阈值史＋as-of 解析）；`advisor_reviews` 三表（0021）；`/api/planning/reviews` 9 端点；建复盘即冻结（as-of 目标版本＋as-of 阈值带＋已采纳建议批次，无史→`REVIEW_BAND_HISTORY_MISSING` 不回退）、四类归因、结构化提案、采纳只记决定、关闭结论必填。
- **前端三期页面**：`/planning/goals`（目标/版本时间线/激活归档设主/所需年化）、记录页 item 级归属 Sheet、`/planning/advice`（收件箱/门/信息集/裁决/设置）、`/planning/reviews`（冻结快照/归因/提案/关闭）；导航规划区新增「目标与达成度/建议/复盘」；首页与分析页新增 `#signals` 域灯条＋异动。

### 变更（有意）
- `/api/metrics/status` 仅加性字段（不破坏旧契约）；C2 `criterion.kind=dual_threshold`；C1 `/progress` 加 `grade`。

### 测试与验收
- 后端 `pytest -q` → **768 passed**；`tsc --noEmit` 0；lint **17 error/8 warning**（存量）；`tokens:check` 137 同步；`check-public-safety.sh` ✅。
- 迁移 0015~0021 均幂等双跑；advisor 隔离与账本零写入有测试断言；复盘采纳后 Goals/AllocationTargets/账本零变化已断言。
- 前端矩阵：三期四页 × 320/390/768/1024/1440（DPR2）无溢出；bundle：目标 276／建议 275／记录 292／分析 341／首页 135／设置 121 kB。

### 已知限制（记账）
- 建议生成为同步执行（job 行已备后台化）；自动触发默认关闭；LLM 占位符门严格（模型写数字即整单失败）。
- 审计链只能发现不一致，无法阻止有库写权限者重算全链。
- 复盘 `allocation_target` 提案暂需手填配置行 id；建议/复盘列表为补摘要逐条拉详情（≤10 请求/页）；异动确认/已读/推送未做。
- 部分录入（沿用值）真库冒烟仍待人工执行。

## [5.8.0] - 2026-10-29

### 新增
- **UI 设计系统与信息架构 v1.0 收官**（design `.scratch/ui-foundation/design.md` v1.1、IA `ia.md` v1.0、审计 `audit.md`；零新增依赖，反向删依赖）：tokens 单源（`styles/tokens.json` → 137 个 CSS 变量 ＋ Tailwind/antd 派生，`tokens:build/check` 校验）／AppShell（桌面侧栏 224/64、1024–1279 自动收栏＋overlay、<1024 顶栏＋底部 Tab＋抽屉；跳过链接、`aria-current`、四边安全区、`dvh`、焦点陷阱/Esc/还原/inert）／基础组件（`PageContainer`/`PageHeader`/`Card`/`Section`/`KpiCard`/`Money`/`DirectionText`/`States`）／数据组件（`DataTable` 排序分页筛选选择、<768 卡片、44 行操作；`ResponsiveDialog` 全屏 sheet；`ConfirmDialog`/`FilterSheet`/表单域）／设置中心（5 区＋二级导航，6 个字典真身，预警规则/结果职责分离，备份/LLM/来源组迁入）／`lib/format` 唯一格式出口（金额中性、负号 U+2212、日期/比率统一）
- **信息架构重构**：顶级导航 6 项（总览/记录/分析/规划/数据/设置）；移动 4 Tab＋「更多」；设置独立固定入口；7 条旧路由 308（query 保留）＋14 条深链兼容；新增 `/data`、`/settings` 5 区索引
- **平台通用性**：一套规范覆盖 Web 桌面/手机 H5/小程序 WebView（320 硬支持、无 hover 依赖、安全区、软键盘、`dvh`/`visualViewport`）；WebView 导出降级提示并禁用；CSS 变量/语义 token 可映射未来原生端

### 变更
- 页面迁移：首页/分析/目标配置（W1b）、记录（DataTable 替换 ProTable）、支出/截图/导入/导出/分配详情（W2c）、设置与字典（W2b）、备份/LLM/来源组移动卡片化（W3）
- 颜色语义统一：涨红/跌绿**只用于变化量**，金额本体中性；max/min、明细方向色走 `DirectionText`；旧 `success/danger` 反向别名清除
- 依赖：删除 `@mui/*`＋`@emotion/*`（4 项）、`@ant-design/pro-components`（53 包）；`echarts-for-react` 维持精确 3.0.6
- 版本单源：页脚读 `package.json.version`（`NEXT_PUBLIC_APP_VERSION` 注入）

### 清理与验收
- 全站清理：`text-slate-400` 文本 **91→0**、10/11px 字号 **48→0**、`formatAmount` 调用 **24→0**（shim 删除）、展示层 `toLocaleString` 清零、`window.open` **2→0**、antd 弃用属性清零；`text-muted` 调至 4.69:1
- 矩阵：27 路由 × 320/390/768/1024/1440（DPR2）无横滚/截断/竖排；键盘/Esc/焦点还原/reduced-motion/44px 抽检通过；正常页 console 0 error
- Bundle（First Load JS）：首页 134 kB、分析 340 kB、记录 289 kB、设置 121 kB、分配详情 324 kB（预算内）；支出/导出 309/304 kB 记账为遗留
- lint Error **100→17**（余量为存量 `any`/unused 技术债）；`tsc` 0；`tokens:check` 通过；build 通过

### 文档
- 新增 `docs/design` 侧：`.scratch/ui-foundation/{audit,design,ia}.md` 与 `u0/w1a/w1b/w2a/w2b/w2c/w3-report.md`、票 `issues/01–05`；ADR：无新增（UI 决策走 design/IA 冻结）

## [5.7.0] - 2026-10-28

### 新增
- 可视化层 v1 收官（design `.scratch/viz-layer/design.md` v1.0.1、spec v1.0 六波 V1–V6；**零新增依赖**）：图表代码分层——`charts/registry.ts`（唯一 ECharts 注册出口）／`charts/theme.ts`（ledger 主题：色板/字阶/动效/高度 token，红涨绿跌只用于变化量）／`ChartFrame`（统一图帧：标题/as-of/灯/口径徽标/图例/四态/数据表/下钻；灯只在帧头、⚪ 不改几何、率不可得断线不落 0）／`presets/*`（纯函数构造 option：composition/trend/comparison）／`primitives/*`（`CompositionChart`/`TrendChart`/`ComparisonChart`）／`lib/format.ts`＋`charts/format.ts`（唯一格式出口，负号 U+2212）／`lib/metric-language.ts`／`charts/meta.ts`（`frameBadgesFrom` 四档折叠）／`charts/environment.ts`（容器宽/密集档/触屏/reduced-motion）
- ECharts 5.6→**6.1**（`echarts ^6.1.0`，`echarts-for-react` 保持精确 3.0.6）：M-1 隔离升级先行——39 张基线矩阵逐像素校验（32/39 全等，7 张资产结构图标签 1–3px 位移 ≤0.09%，按 design §7.2 记为允许），旧图零回归；M4 拆除 `v5-compat` 兼容层（主题/LegacyGridContainLabel/`outerBoundsMode:'none'`），`Chart` 唯一主题 `ledger` 并移除 `theme` prop
- 页面迁移：分析页占比（资产/负债 × 饼/环/玫瑰/树图/旭日，窄屏回退 donut）与趋势（brush/dataZoom → 窗口 chips；含沿用值空心点/斜纹；极值标注；明细恒全量）；首页趋势＋资产/负债 treemap（删 `DistributionList`/`PartialLegendHint` 及死代码）；分配 `[dimension]` 偏离图改为横向棒棒糖（零线/±阈值 amber 带/逐行读数/全量行，删 `DeviationChart` 138 行）
- 无障碍与口径：图帧 `figure`/`aria-labelledby`/body 摘要、数据表 caption/作用域/键盘、图例 44px＋`aria-pressed`、reduced-motion 关动画；口径徽标随帧（阻断→来源→判据→一般折叠 3 档），降级不脱图
- ESLint `no-restricted-imports` 升 **error**：页面只允许从 `@/components/charts` 取 primitive/类型（全站 0 违规）

### 变更（有意，design §6.5）
- 标题左对齐、图例入帧、轴标签 slate-500、金额轴万/亿紧凑、treemap 品牌色阶、参考线 muted 虚线、tooltip 深墨、负号统一 U+2212（MetricCard 归一）；分配页不截断行数致页面变长（高度随行数增长）

### 测试
- 前端 `tsc --noEmit` 零错误；全量 lint 83 error/10 warning 全存量（V6 顺带消除分配页 15 项），charts 层与迁移页零新增
- 截图矩阵：V1 39×2、V3 30 图、V4 18 帧、V5–V6 全站 18 页 × 390/768/1440 × DPR2（本地 `/tmp` 不入库）；四态/键盘/reduced-motion/容器 resize/390 手势逐波验证
- 后端 pytest 全量 **554 passed**（后端零改动，回归锁定）；`bash scripts/check-public-safety.sh` ✅

### 文档
- `.scratch/viz-layer/`：design v1.0.1、spec v1.0、选型调研 `research-2026-10.md`、M-1/M0–M4 实施报告、票 01–06
- `docs/design/README.md` 阶段行同步（可视化层 v1 收官）

## [5.6.0] - 2026-10-26

### 新增
- 指标骨架（三期-0，规格 `.scratch/metric-skeleton/spec.md` v1.0；**零迁移、零新表**）：`backend/services/metric_service.py` 指标注册表（代码即真源，启动自检 id 唯一＋标签枚举合法）＋ `backend/routers/metrics.py` 状态面编排；首版 11 项指标全部复用现有计算（B1 总资产/总负债/净资产/环比、底座覆盖度、B2 升值额/升值率/TWR/XIRR、B3 负债占比、C2 配置偏离）
- 灯引擎（纯函数，可单测）：输入（值、判据、闸门集合）→ `green | yellow | red | gray` 或 `null`——阻断性闸门先判 ⚪；无判据即 `null`（不判灯、不预置默认带）；本期仅绝对阈值型偏离（`allocation_targets.alert_threshold`，`|deviation| > threshold → 🟡`，否则 🟢）；`band_basis` 恒 `current`（历史定格留三期-4）；判据类型枚举预留单向/双向带/双阈值/状态型
- 口径闸门映射：`meta.degradations`＋局部降级 → `gates[{code, note, blocking}]`——`NO_DATA` 全项阻断、`RATE_UNAVAILABLE`（twr/xirr）阻断、`PRINCIPAL_NOT_SET` 注记（组合不可得时阻断）、`DATA_MISSING_CARRY_FORWARD`/`TWR_LARGE_FLOW_APPROX`/部分录入注记；**口径降级的指标永远不给绿**
- 端点：`GET /api/metrics`（字典，静态无 meta）＋ `GET /api/metrics/status?as_of=`（值＋灯＋闸门＋meta）；as_of 默认最新快照日、越界 422；数值一律字符串 Decimal（金额 2 位、比率/收益率 4 位，% 前端格式化）；同一 as_of 下与 summary/returns/appreciation/allocation 对账一致
- 前端：分析页顶部指标总览条（桌面横排、移动紧凑纵向）＋ `MetricCard`（值＋四色灯点，无判据显示方向文字，闸门折叠徽标，点击下钻）＋ 通用口径徽标 `components/ui/MetaBadge.tsx` 挂概览页净资产区域（与部分录入批次 `PartialBadge` 暂并存，合并留后续清理）；`lib/api.ts`/`types/index.ts` 增调用与类型，不改现有图表数据逻辑

### 测试
- 后端新增 `test_metrics.py` 14 用例：注册表自检＋字典契约、灯引擎真值表、状态面契约与精度、与现有端点对账、覆盖度/部分录入闸门、空库全 ⚪、率不可得 ⚪、本金未设注记/阻断、大额流近似注记、路由、as_of（默认/指定/越界）、偏离阈值行为、无目标缺行；全量 pytest **554 passed**（540→554）
- 前端 `tsc --noEmit` 零错误；`next lint` 新组件零告警；本地联调冒烟通过（桌面/移动、徽标展开、TWR→双收益下钻、概览深链、空库 NO_DATA/⚪）

### 文档
- `CONTEXT.md` 增补指标字典、指标状态、判据带·灯、口径闸门四词条；`docs/design/README.md` 阶段行同步（指标骨架实现落地）

## [5.5.0] - 2026-10-20

### 新增
- 血缘链接（v5 二期·子步 2.4，二期收官项）：`0014_lineage_links` 表——`link_type` CHECK ∈ `merge|split|revision`（**无 rename**，拍板 10 改名走显示名史）、`split_ratio` NUMERIC(6,4) NULL（仅 split，(0,1] 服务层校验）、append-only 双触发器＋from/to 双索引（同 0009/0012/0013 模式，全守卫幂等）
- `POST /api/lineage-links`（幂等键）＝单事务用例编排，**失败安全序**：校验全部前置（旧项存在/active、目标名未占用、ratio、承接值 Decimal、reason 必填 R-9）→ 新项创建（`get_or_create_item`）→ 每旧项一条链 → **口径承接**（新项初始 `principal_changes` `origin='lineage'`——0012 预留值启用、reason 引链 id、值＝**预填人确认**服务端不复算）→ 审计 `lineage_link`×n＋伴生 `principal_set`（单点行形状）→ **旧项归档最后**（`set_item_status` 既有状态机，自带 archive 审计）；链**创建即固定**、无 update/delete 端口（拍板 8），承接值录错＝对新项追加更正
- 只读 `GET /api/lineage-links?item_id=`：**双向** predecessors（前身）/successors（后继）；不给 item_id＝全链列表；视图边界守住——不拼接跨项收益、无 rename 路径
- 计算衔接（零改动天然成立，回归锁定）：`lineage` 不入组合/item 收益现金流、不入归因外部段——血缘登记前后组合三率全等；归档旧项历史行仍按「当时在场」入期间序列
- 前端：升值 tab 逐项表「血缘」动作弹窗（三类型切换／merge 多选并源／新项名／split ratio／**建议承接额联动预填可改**（口径未设按 0）／理由必填／备注）＋本项链（前身/后继）展示＋「旧项立即归档、链不可改」不可逆警示；成功后联动刷新升值/收益/归因

### 测试
- 后端新增 `test_lineage.py` 11 用例：revision 全形状（归档联动＋origin/reason 引链 id＋审计三件齐）、split ratio（0.6000 落库、越界/缺失/误带三拒）、merge 多旧项（两链一承接、双向前身两条）、rename 拒绝、五守护（名占用/不存在/已归档/reason 空/空列表）、0014 双跑对象恰 5 不倍增、触发器 UPDATE/DELETE＋link_type CHECK 均 ABORT、**组合收益前后三率全等关键回归**、item 收益与归因外部段不含承接、双向列表、非法日期/金额形状；全量 pytest **531 passed**（520→531）；真库副本演练 **19/19**；前端 `tsc --noEmit` 零错误

## [5.4.0] - 2026-10-20

### 新增
- 净资产变动归因（v5 二期增强，规格 `.scratch/v5-attribution/spec.md`；只读派生、零迁移）：`GET /api/analysis/attribution?scope=portfolio|item&id=&period=&period_type=&date_from=&date_to=`（窗口＝period ＞ 显式区间 ＞ 全历史）——四段恒等式 **`Δ净资产 = 外部净投入 + 内部转移净额 + 内生变动`** 定义性成立（全程 Decimal 2dp，**内生＝残差**，吸收市值涨跌、未覆盖项与始末快照时滞，assumptions 明示勿读作「投资收益」）；始末净资产＝R-2 回溯口径（复用 appreciation 的 `_active_rows`，资产−负债与 summary 一致）＋**回显实际取值快照日**（carry-forward 时滞显式化）；外部流＝origin='external' 增量沿全链计算、半开区间 **(start, end]**（start 当日不计）、**按 side 定号**（负债侧口径增加为负——借贷场景 Δ=0/external=0 锁定）；转移段组合层结构性净零 `amount="0.00"`＋`gross`＝单边转移总额＋note，item 层＝该项净额（无 2.3 数据自然退化为 0）；`coverage{principal_set_ratio(4dp), uncovered_items[]}`＋复用降级码 `PRINCIPAL_NOT_SET`/`NO_DATA`（不新增码）；`meta.basis.principal="pc-1"` 照传；参数错误 422 `VALIDATION_ERROR`（details.fields 定位 scope/id/日期）、项不存在 404
- `/analysis` 升值 tab 新「期间归因」卡：窗口双日期选择器（缺省全历史）＋Δ 核对行（始→末＋实取快照日）＋分段列表（external 含流入/流出、transfer 含 gross/note）＋核对等式行＋覆盖率与未覆盖项清单＋口径假设折叠；设定口径/登记转移成功后联动刷新（瀑布图按规格后置）

### 测试
- 后端新增 `test_attribution.py` 10 用例：恒等式 Decimal 精确对拍（组合 `730＝500＋0＋230`／item `330＝0＋300＋30`／`−200＝0＋(−300)＋100`）、借贷场景 side 定号（inflows 500／outflows −500）、窗口半开区间边界（start 当日不计、end 当日计）、转移组合净零＋gross／item 净额／退化版无 2.3 数据、coverage `0.5000`＋`PRINCIPAL_NOT_SET`、空库 `NO_DATA` 单挂、carry-forward 始末日期回显（含不对称窗口）、**只读零变更**（五表行数前后全等）、路由 422/404/账期窗口；全量 pytest **520 passed**（510→520）；真库副本只读演练通过（恒等式 Decimal 精确成立；覆盖率/降级/只读零变更）；前端 `tsc --noEmit` 零错误

## [5.3.0] - 2026-10-20

### 新增
- 转移事件（v5 二期·子步 2.3）：`transfer_events` 表（amount **恒正** CHECK，方向由 from/to 表达；0013 迁移＋append-only 双触发器，同 0009/0012 模式）；登记不变量＝**两侧本金等额、同事务两条 `principal_changes`**（from −amt／to ＋amt、origin='transfer'、0012 预留列 `transfer_event_id` 双向挂链、reason=「内部买卖转移 #id」，净值不变不产生流水 R-34/拍板 9）；累计基准走 `principal_service.history_basis` 纯历史链（同日多条追加需见「此刻累计」；未设口径按 0 起链）；**更正＝登记反向转移**（追加不改史，无 update/delete 端口）；审计单行 `transfer_register`
- **登记日即快照日**校验（R-34 注）：两侧 item 于 event_date 须各有 active 快照行，缺 → 409 `SNAPSHOT_REQUIRED`（新码，details 携 missing_items **点名清单**＋hint 引导先走 F1/更正补行，补齐可原样重试）
- 端点：`POST /api/transfer-events`（幂等键 guard）＋只读 `GET /api/transfer-events`（按任一侧 item／日期区间筛选）
- 计算衔接：**组合级 TWR/XIRR 排除内部转移**（origin 过滤＋现金流增量改**跨全部 origin 链式计算后按 origin 分类取用**——转移后的外部增量不再错算）；**item 级视转移为该持仓现金流**；returns 纯函数修正中段估值日上的流归属（归「以其为段尾」的段、零权，MD 期末流约定）
- 前端：升值 tab「登记转移」弹窗（两 item 选择／金额恒正／日期默认查看期／理由必填；`SNAPSHOT_REQUIRED` 转弹窗内引导面板逐项点名缺侧）；口径变更史 **origin 徽标**（转移=天蓝／血缘=紫预留／external 默认无徽标）；登记成功联动刷新升值/收益/口径史

### 测试
- 后端新增 `test_transfers.py` 11 用例（等额不变量、同日多条链式累计、快照 409 重试语义、组合排除内部流关键回归 `0.130000` vs item 级 `0.142857`、反向更正净额复原、0013 双跑幂等对象不倍增、触发器/负额 CHECK、列表筛选、审计单点、参数校验）＋ `test_returns.py` 估值日流归属回归 1 用例；全量 pytest **510 passed**（498→510）；真库副本演练 **16/16**；前端 `tsc --noEmit` 零错误

## [5.2.0] - 2026-10-20

### 新增
- TWR 时间加权收益（v5 二期·子步 2.2）：估值序列＝R-2 回溯口径（与 summary/appreciation 同读法）；外部现金流＝本金口径变更史 `origin='external'` 相邻增量（注入正/撤出负，负债侧不计）；**重估日切段＋段内 Modified Dietz 权重（w=(e−d)/(e−s)，段尾流零权）＋几何链接**，年化＝(1+R)^(365/天数)−1；大额现金流（单笔 ≥期初组合 15%）当日无重估值 → 附降级 `TWR_LARGE_FLOW_APPROX`（scope=区间，MD 均匀时点假设误差最大处诚实标注）
- XIRR 资金加权年化：投资者视角定号（期初市值负/注入负/撤出正/期末市值一笔正，勿与逐笔重复）、365 天贴现；牛顿迭代（guess=0.1）＋失败回退二分（倍增找括号区间）；**不收敛或无一正一负 → `rate=null`＋`RATE_UNAVAILABLE`（绝不编数）**；`returns_service` 为 valuation 纯函数模块（零 I/O），组装与降级收集在 `analysis_service.get_returns`
- 端点 `GET /api/analysis/returns?scope=portfolio|item&id=&period=&period_type=&date_from=&date_to=`（只读）：窗口＝账期标签 ＞ 显式区间 ＞ 全历史；响应 `{twr:{period,annualized},xirr,assumptions[],meta}`，meta 携降级、`meta.basis.principal="pc-1"`；scope 非法/缺 id → `BAD_REQUEST`，项不存在 → `NOT_FOUND`
- `/analysis` 升值 tab 并排「双口径」卡：**TWR·衡量策略**（剔除加减仓时点）/ **XIRR·衡量本人操作**（资金进出时点计入）＋「率不可得」态＋大额近似标注条＋口径假设（assumptions）折叠展示；设定口径成功联动刷新收益卡

### 测试
- 后端新增 `test_returns.py` 18 用例：Modified Dietz 手工算例对拍（50/1050＝`0.047619`、年化 `0.3271`）、段尾零权、两段几何链接（`0.210000`）、大额阈值边界（恰 15% 含/149 不含/当日有估值不标）、期初非正与单日降级 reason、XIRR 已知解对拍（0.1±1e-6、双息票 0.065965±1e-4、深负根 −0.996）、无正负对/单点降级、负债侧不计、窗口解析（账期/显式区间/全史）、空库 `NO_DATA`、路由参数校验；全量 pytest **498 passed**（480 基线零回归＋18 新）；真库副本只读演练 ALL_PASS；前端 `tsc --noEmit` 零错误

## [5.1.0] - 2026-10-02

### 新增
- 本金口径（v5 二期·子步 2.1）：`principal_changes` append-only 变更史（DB 触发器拒绝 UPDATE/DELETE；带符号「累计投入本金」注入正/撤出负；reason 必填 R-9；更正＝追加不改史，无 update/delete 端口）＋ `assets.cost_basis` 写时定格冗余（存量行不回填；读端统一 `resolve_cost_basis(item, d)` 行值优先、NULL 回退史，两分支等价性测试锁定）；设定/查询端点 `POST/GET /api/asset-items/{id}/principal`（幂等键＋审计 `principal_set`/`principal_change`，before=旧生效值）；历史视图按当时生效口径定格（R-36）
- 升值：`GET /api/analysis/appreciation?as_of=` 逐项/组合升值额与升值率；行选择＝**R-2 回溯口径（carry-forward）**，与 summary/proportion/allocation 一致；口径缺失或 ≤0 → `RATE_UNAVAILABLE` 诚实降级（首次消费预留码）、组合级 `PRINCIPAL_NOT_SET`（scope=未设项清单）；`meta.basis.principal="pc-1"` 激活口径契约版本
- `/analysis` 新「升值」tab：组合四卡片＋查看期标注＋逐项表＋「设定口径」弹窗（reason 必填、变更史展示、每用户动作一个幂等键）

### 修复
- 升值 tab「设定口径后没有变化」体验陷阱：弹窗默认生效日期由「今天」改为当前查看期 `as_of`；手选生效日晚于查看期时醒目提示「保存后当前视图不会变化，需在 {生效日} 起的视图查看」；保存成功 toast 附生效日

### 文档
- 业务架构追加 v1.2 增补记录（负债侧口径语义注＋本方口径带符号澄清）；CONTEXT 增补「本金口径」词条；二期规格记录修订决议（carry-forward 修正＋修订建议 1~3 处理）

### 测试
- 后端新增 `test_appreciation.py` 18 用例（解析多段/同日 id 决胜/行值等价性/历史定格/追加更正/append-only 触发器/降级/端点审计/carry-forward 回归）；全量 pytest **480 passed**（462→478→480）；真库副本演练 23/23；前端 `tsc --noEmit` 零错误

## [5.0.3] - 2026-10-02

### 新增
- 截图上传「存证前置」（v5 一期·步骤 6）：上传即落凭证（原图 + SHA256，先于任何识别，落盘/建行失败 → 500 `EVIDENCE_WRITE_FAILED` 阻断识别）；同图去重（同 sha 一行一文件，preview→apply 重传天然复用）；preview / preview-jobs 响应新增 `evidence_id`（additive，预览阶段即可引用凭证 #id）；孤儿凭证只读查询 `GET /api/screenshots/evidence/orphans/list`（未采纳且无资产引用，仅读无删除端口）
- 预览任务落库续跑：`0011_preview_jobs` 表（含续跑 payload：从凭证 `file_path` 取图重跑，识别只读天然幂等）；lifespan 启动钩子自动恢复 queued/running 任务（陈旧 running 复位重入队）；进度节流落库（status/stage 变迁必写，高频行进度按 ≥2s 或 ≥20 行合并，写失败仅告警不阻塞）；done/error 终态必落库、内存淘汰后轮询回落 DB（字段形状零变化）；done/error >7 天 TTL 日清（挂既有 scheduler）

### 变更
- 新凭证按**上传年月**落位且不再随 data_date 移动（`file_path` 列为真源，data_date 为 DB 属性；旧凭证按 data_date 组织，混存无碍）；apply 由「新建凭证」改为对复用凭证**富化**会话属性（data_date/anchor/supplement_rows/engine 随最近采纳覆盖；文件/sha/created_at 永久不可变）
- 留存说明：孤儿凭证（预览过又放弃）按 R-11 永久保留——「预览过又放弃」本身是录入过程证据

### 测试
- 后端新增 `test_ingest_fix.py` 12 用例（存证/去重/富化/阻断/续跑演练/节流/轮询形状/0011 幂等）；全量 pytest 462 passed（450 存量零回归）；前端 `tsc --noEmit` 零错误（仅 types/api additive）

## [5.0.2] - 2026-10-01

### 修复
- `/records` 列表「新旧行同屏」：更正＝追加语义下，被更正历史行与承接新行曾并列显示。A/B 决策取 B——保留追加写语义（不修订 SDP-2 / R-4 / 不变量 2），由视图层解决：
  - 后端 `list_assets` 默认仅返回 active 行（`superseded_by IS NULL`），新增 `include_superseded` 参数（默认 false）返回全部；列表端点 query 透传，响应向后兼容新增可空链字段 `superseded_by` / `is_superseded`
  - 前端桌面工作台新增「显示历史（含被更正）」开关，历史行弱化呈现（半透明＋删除线＋「已更正」标签）；移动端卡片流恒 active

## [5.0.1] - 2026-10-01

### 修复
- `/records` 编辑弹窗组合保存（属性 PUT＋金额 correct＋改名 rename 三个子请求）共享同一幂等键，第二个子请求必触发 422 `IDEMPOTENCY_KEY_REUSED`——前端改为**每请求一键**（`idemKeys` 在提交点一次性生成一组、随 mutate 变量传入，react-query 重试复用同组键，重试去重语义保留）

### 新增
- 批量修改支持「资产/负债」侧别：`POST /api/assets/batch-update` 携带 `side`＝逐 active 行按**更正追加**语义处理（金额不变、同日追加新行＋旧行 `superseded_by`、审计 correct；同侧与历史行跳过；携带 side 时 `updated_count`＝实际翻转条数），前端批量修改弹窗同步该字段并注明追加语义
- 错误码 `INVALID_SIDE`（400，「无效的侧别（仅 asset/liability）」）入契约枚举，准信封 `invalid_side` 精确映射（不再落 GENERIC_400）；`InvalidSideError` 专用异常，批量更新路由收窄捕获面（其余 ValueError 不误标 400）

### 变更
- 步骤 3 契约修订（`.scratch/v5-identity/spec.md` 追加修订记录）：`side` 由「409 禁改」改为「批量 append 更正」合法调用面；单条 PUT 直改 side 仍 409

## [5.0.0] - 2026-10-01

数据世代 V5（一期根基重构，五步全部落地；规格归档于 `.scratch/v5-backup-fix|v5-migration|v5-identity|v5-audit|v5-contract/spec.md`）。

### 新增
- **备份 bundle**：备份＝单一 zip（manifest + SQLite 在线一致快照 + `evidence/` 凭证同备，原子落盘）；恢复前自动生成回滚包、WAL 伴生文件清理、凭证只增不删合并；服务端保留 N=5（`rollback_*` 永不删）；旧裸 `.db` 兼容恢复（响应附「不含凭证」警示）
- **编号迁移机制**：`schema_version` 记账表 + `backend/migrations/0001~0010` 编号脚本 runner（每脚本一事务、幂等守卫、失败 fail-fast 中止启动、重启自动续跑）；增量执行且有数据时迁移前自动备份（`reason=pre_migration`）；v4 老库基线认领（0001~0007 不重复执行）
- **身份层（V5 核心）**：`asset_items`（资产项＝持久 ID 聚合根，display_name/channel/status/legacy_name）＋ `asset_item_names`（显示名史，append-only）；`assets` 重建：`asset_item_id`（NOT NULL）＋ `superseded_by` ＋部分唯一索引 `(asset_item_id, asset_date) WHERE superseded_by IS NULL`，旧 `(asset_date, asset_name)` 唯一约束移除（id 原值保留、行数与逐日净资产合计经校验断言保证不变）；新资源 `GET /api/asset-items` 与 rename/archive/unarchive 端点、`POST /api/assets/{id}/correct`
- **审计日志**：`audit_log` 表（append-only，存储层触发器拒绝 UPDATE/DELETE）覆盖更正/采纳逐行/改名/归档/建删/迁移执行六类写路径；与 request_id、幂等键并列落痕；只读端点 `GET /api/audit-log`（筛选＋分页）
- **契约层**：统一错误信封 `{"error":{code,message,details}}`（14 错误码枚举 + 存量 HTTPException 包装器兜底）；`Idempotency-Key` 幂等键（39 条写端点挂载，同键同体重放、异体 422、7 天 TTL 日清）；计算类读响应口径元数据 `meta`（snapshot_date / basis{schema_generation:5, identity:asset_item} / degradations，含 NO_DATA / DATA_MISSING_CARRY_FORWARD；RATE_UNAVAILABLE 二期预留）
- 备份 bundle manifest `generation` 世代位填 5（V5 收官标记）

### 破坏性变更
- `PUT /api/assets/{id}` 修改金额/负债侧/快照日 → **409**（write-once；改金额走 `POST /api/assets/{id}/correct`，改名走 asset-items rename）
- 批量创建/CSV 导入的 `overwrite` 静默覆盖**移除**（冲突返回显式 `skipped[]` 清单；单条 create 撞同日同项 → 409 SNAPSHOT_CONFLICT）
- 全部非 2xx 响应形状由 `{"detail":…}` 切换为统一错误信封（前端同窗一次切换，单客户端；旧 `warnings[]` 等成功体附加字段保留）

### 文档
- `docs/MIGRATION.md` 增「V4 → V5」段（变更清单/备份恢复指引/升级与回退/FAQ）
- ADR-0006 编号迁移与迁移前备份；ADR-0007 append-only 审计日志；ADR-0008 契约层信封与幂等键
- CONTEXT 增补 V5 术语（资产项实体、显示名史、更正、审计日志、幂等键、口径元数据、Schema 版本）

### 测试
- 后端新增 `test_backup.py`（9）/ `test_migrations.py`（7）/ `test_identity.py`（14）/ `test_audit.py`（15）/ `test_contract.py`（17）；全量 pytest 436 passed；前端 `tsc --noEmit` 零错误

## [4.1.0] - 2026-09-26

### 新增
- 支出录入：固定支出（模板 + 一键生成当期记录，可改实际金额/日期，`(template_id, period)` 幂等）与临时大额支出（可关联账户）；金额正数、字符串序列化
- 支出分类维度：独立 `expense_categories` 表与管理页；被引用分类删除守卫 400
- 支出汇总：本月/本年合计、按分类聚合、固定支出月负担（月 + 季/3 + 年/12）；首页「本月支出概览」卡片（点击跳转）
- 前端新页 `/expenses`（支出流水 + 固定支出项目双 tab、生成向导弹窗）与 `/expense-categories`

### 文档
- ADR-0005：支出流水独立于资产快照（支出不参与净值计算）
- CONTEXT 补充支出术语（支出记录/固定支出/临时大额支出/固定支出项目/支出分类/固定支出月负担）

### 测试
- 后端新增 `test_expenses.py` 24 用例；全量 pytest 374 passed；前端 `tsc --noEmit` 零错误

## [4.0.0] - 2026-09-19

### 新增
- 大模型供应商配置：新增/编辑/删除供应商（base_url、API key、模型名、默认标记），预置 DeepSeek 与阿里云百炼，key 脱敏展示、不进前端 bundle
- 截图 AI 一致性校验：视觉大模型独立看图输出识别结果，与 OCR 结果逐行比对（一致 / 分歧 / 单边缺项），支持手动与自动触发、按截图 SHA256 缓存、失败静默降级
- 补录通道：AI/人工补录项经「采纳」后按名字键/内容键落库，含审计
- 识别引擎对等 v1：`engine=ocr|ai`，AI 可作骨架识别来源；写入不变式升级为「只认用户确认的待录入清单」
- 账户维度录入提醒：按账户提醒 + 一键录 0 + 首页录入状态横幅
- 源分组管理界面与分组规则服务

### 改进
- 截图解析管线重构：异步预览任务（preview jobs）、OCR 引擎多分块 + 重叠窗口 + 透视校正
- 结构化日志与请求上下文（x-request-id）

### 测试
- 新增 llm_verification / peer_engine_modes / account_reminder / preview_jobs / parser_amount_binding / rules_write_paths / screenshot_date_contract / guide_image_upload / copy_from_last 等测试

## [3.1.0] - 2026-08-30

### 新增
- 截图录入收尾（A6）：源分组 CRUD（账户多对多）、通用解析器 organize blocks、未识别内容的规则分发策略、前端录入流程重排、写入与凭证打通
- 纠错规则反哺层（ADR-0004）：apply_correction_rules 纯函数、规则端点、前端保存规则开关

## [3.0.0] - 2026-08-22

### 新增
- T2 截图→待录入清单后端全链（解析 + 映射 + 预览，接缝 A 单测 14 项）
- T3 确认→写入 + 凭证（凭证实体、挂接、属性继承，接缝 B 测试 5 项）
- T4a 截图录入 API（preview/apply/evidence、SHA256 解析缓存、apply_user_edits）
- T4b 截图录入前端页（上传 → 预览 → 确认写入 + 凭证）
- T5 截图录入 CLI（默认只出清单，`--apply` 写入 + 凭证；真截图验收 18 项全中、对账 0.00）

### 变更
- OCR 引擎选型确定：RapidOCR（准确率 ≈100%）

### 文档
- 截图录入 spec（A2）与拆票（A3）；CONTEXT 补充「截图凭证」术语

## [2.13.0] - 2026-08-22

### 变更
- 资产/负债金额统一为正数，新增 side 字段区分方向（ADR-0002）

### 文档
- 重建原始需求记录（P1–P4 / 领域模型 / 阶段 / 验收）与 2026-08 进度对照

## [2.12.0] - 2026-08-20

### 移除
- 彻底删除 V1 遗留：asset_records 表、死代码、死 schema 与 DROP 迁移
- 前端清理 V1 残留（upload-db 仅保留 restore，删除 5 个死类型与迁移分支）

### 修复
- 修复 cron 契约错位
- 修复 6 处 antd v6/dayjs 迁移错误；重新启用 TS 构建检查；CORS 关闭 credentials

### 重构
- 收敛比例计算重复代码为命名 helper

### 测试
- 新增最小后端测试：比例计算 + CSV 导入落库（5 用例）

### 杂项
- 删除孤儿测试 useAllocation.test.ts；移除误提交的 tsconfig.tsbuildinfo（gitignore 忽略）

## [2.11.0] - 2026-08-19

- 导入真实 person_fin 代码，重建本地基准（取代陈旧 person_f 快照）。此版本号为导入基线。

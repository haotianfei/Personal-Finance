# 功能开发计划

本文档用于记录希望实现的功能和开发计划。

> **入口地位**：本文件是文档驱动开发的 **G0 意图登记处**（见 `docs/WORKFLOW.md` §4）。
> 每条待实现项在被实现前须先定级（L0–L3，§3）：**L1 及以上必须先有 `.scratch/<slug>/spec.md` 并冻结**，然后才能开票与动代码；L0 免 spec，按清单判定（§3.1）。
> 条目写清「改完之后用户能做什么以前做不到的事」，不写实现方法（那是 spec 的事）。

## 待实现功能

### 高优先级

- [x] 资产记录功能添加多维资产搜索过滤功能，例如按资产类型、资产名称、资产状态等
- [x] 趋势图增加时间范围选择功能，用户可以自定义查看时间范围内的资产趋势数据，默认不超过近一年，以免数据量过大导致性能问题
- [x] 历史属性添加资产拥有者字段——item 级归属编辑/历史（`asset_item_owner_changes`，0016）
- [x] 批量修改历史归属（`POST /api/asset-items/owners/batch`，逐项独立事务、同值 skip、归档拦截）

- [x] **预警维度同义词漏加 join（已修，票 01）**：`_sum_at` 用原始维度判断是否 join，
  而 `_condition_for` 内部归一化——旧拼写 `risk` 下条件落在 `asset_items`、join 却没加，
  `FROM assets, asset_items` 交叉积把「单档合计」放大成总资产（实测 600 vs 100）。
  已在 `_sum_at` 入口归一化；`test_alert_asof.py` 补真实命中断言并做过反向验证。
- [x] **归因覆盖率分母含归档项（已修，票 01）**：`coverage.principal_set_ratio` 的在场项集合
  走 `appreciation_service._active_rows`（不看 `AssetItem.status`），已归档项被算作「在场未申报」，
  拉低 ratio 并在 `uncovered_items` 里点名已退场的项。现叠加 `measuring_at` 与 `_net_at` 同谓词。
  不影响净值，只影响百分比与降级提示。
- [x] **配置分析重复取数（已合并）**：一次 `/api/allocation/analysis` 内 `get_total_assets` 与
  `_amounts_by_item_column` 各取一遍同一份 as-of 记录。现新增 `_as_of_records()` 供两者复用
  （可选 `records=` 参数，向后兼容）；合计仍由 `_aggregate_records` 产出，口径零变化。

### 中优先级

- [x] **三环境（dev / staging / prod）+ CI 门禁 + 备份恢复演练（2026-11-09 登记，G0）**：现在只有「源码手跑」和「deploy 一把梭」两档，没有预发、没有上线前门禁、没有版本化镜像与回滚点、没有定期恢复演练。改完后能做到以前做不到的事：上线前在 staging 用**在线库副本**（backup API 取，绝不动在线库）预演一遍；`release.sh` 自动跑 CI、打 `:YYYYMMDD-<shortsha>` 镜像与回滚点、失败自动回滚；备份每日定时并保留 14 天，季度恢复演练产出报告。**L2，须先冻结 `.scratch/env-tiers/spec.md`**；计划与 7 张票见 `.scratch/env-tiers/map.md`。
  - **已完成 2026-11-09**（spec 已冻结 + ADR-0030）：三环境端口 dev 6679/6678、staging 6689/6688、prod **6699/6698**（已由 6789/6788 切换）；`release.sh` 真实发布 `20261109-aa43d66` 双 tag + smoke 6/6，`rollback.sh` 实跑回退成功；`prune-images.sh --apply` 在用镜像守卫生效；`ci.sh` 三档（880s/63s/872s）+ pre-push；备份保留策略 + user timer 已 enabled（每日 03:00）；恢复演练首跑 PASS（`.scratch/env-tiers/drills/20261109.md`）。残留缺口见 spec §3 第 8/9/15 条括注与 `.scratch/env-tiers/report.md`。

- [ ] **开发库与生产部署库的路径/文件名区分（2026-11-09 登记，G0，方案 A）**：两边现在都叫 `person_fin.db`，只靠目录区分（开发 `data/person_fin.db`、部署 `deploy/data/person_fin.db` → 容器 `/app/data/person_fin.db`），`DATABASE_PATH` 指错不会有任何提示。改完后开发库叫 `data/person_fin_dev.db`、启动日志自己报 `db_role=dev|prod`，一眼能分辨手上连的是哪套库。**L1，须先冻结 `.scratch/db-dev-suffix/spec.md` 才能开票**；部署侧 `deploy/**` 按裁决不动。

- [x] **截图 OCR 多列表版式误判导致整图只剩 1 项（2026-11-07 登记，G0）**：示例银行截图（9 个资产项）经 OCR 管线只吐出 1 项「示例活钱A ¥10,000.00」，AI 侧 9 项齐全。根因是 `_detect_layout` 的 multi_column 签名用**原始文本**比金额互异，而 OCR 把同一行出了两份（`"10,000.00 >"` / `"10,000.00>"`，仅差尾部空格）→ 整图判为多列 → `_organize_multicolumn` 要求每行 ≥2 金额，其余行全丢。改完后用户能做的以前做不到的事：同一张图不再需要点「转用 AI 识别」补录 8 项。**须先冻结 `.scratch/multicolumn-signature/spec.md` 才能开票动代码**（该改动被 `parser-rules-refactor` §4 不做第 6 条明确排除，不能按 L0 走）。票外发现另记：① `示例数字钱包` 卡片「余额」续行被当独立项；② exclude 用全名精确匹配，`示例银行-示例数字钱包 尾号0000` 未被 `-示例数字钱包` 排除。
  - **2026-11-07 收口**：签名改为按 `_cell_amount` 解析的 Decimal 判异 + 要求 ≥2 行支持（同图由 `multi_column` → `horizontal`，1 项 → 10 项、合计与图中「总资产」块逐分相等）；`_is_excluded` 由全名精确扩为「精确 或 match 后紧跟非字母数字」（Unicode `isalnum` 判据，无字面量字符集），票外发现②随修，18 条资产仅 1 条口径变化即预期收益；① 按业主裁决**不写任何教学规则**（ignore 是子串匹配，会静默吞掉真实资产行），两条 0.00 行保留为界面可取消勾选的 update 行，**零数据变更**。新增 `backend/tests/test_layout_signature.py`（15 例，旧实现上 6 红 9 绿）与复现工具 `scripts/repro_ocr_layout.py`；`_organize_*` 与等价矩阵断言一行未改。spec / report 见 `.scratch/multicolumn-signature/`。
- [x] **设置页与全站内容宽度统一（2026-11-06 收口）**：上限由 `tokens.json` 的 `shell.content-max` 单点决定，改为 `clamp(1600px, 88%, 1920px)`；设置页收进 `PageContainer`、不再硬编码 `max-w-[1200px]`，全站 38 个页面只剩一条宽度规则；四个零消费 token（`settings-max`/`form-max`/`sidebar-expanded`/`sidebar-collapsed`）已删。实测：2560 视口设置页内容 1856px（上限 1920 封顶、未被套两层）、1440 视口主列 928px，业务页 1440 下与改造前逐像素相同。**已知局限**：收益随屏幕宽度递增——1024/1280/1366 为 +0、1440 仅 +16、1600 起 +176、1920 +400、2560 +720；1440 屏若仍嫌窄，需另动内边距（桌面 32）或侧栏（224），属新决策。spec `.scratch/layout-width/spec.md` 已冻结，票 01–03 全 resolved，提交 `dcafe44` / `1793a94` / `88d838d`。
- [ ] **记录页筛选/排序/页码迁入 URL（`record-provenance` 票外发现，2026-11-06 登记）**：从记录行点开凭证详情页再后退，筛选/排序/页码会全部重置——它们只活在组件 state 里，URL 当前只承载 `date` / `institution_id` / `mode` / `view` / `highlight` / `evidence_id`。改完后「后退回到原视图」成立，链接也能复现同一屏。属 G0 意图，**须先有 `.scratch/<slug>/spec.md` 并冻结才能开票**（G2 在 G1 之后）；`record-provenance` 的 spec §4 已把它记为已知限制，不在该轮范围。
- [x] **LLM 供应商设置两项（2026-11-06 收口）**：①测试结论已落库——`llm_providers` 增四列 + 迁移 `0034`（ADR-0025 记录了「`NULL`＝从未测试 ≠ `false`＝测过且失败」和「`base_url`/`model`/`api_key` 的值变更即作废」两条判据），前端为行内三态徽章；②默认行已加「取消默认」入口（后端本已支持，缺的只是可见性）。spec `.scratch/llm-provider-status/spec.md` 已冻结，票 01–03 全 resolved，提交 `d4cd709` / `0632935` / `a23f9b3`。
- [ ] **凭证来源组彻底去名字化（收尾项）**：`screenshot_evidence` 已改存 `source_group_id` 外键，但仍有两侧残留字符串口径——① `preview_jobs` 的续跑 payload 里存的是组 slug，组改名后重启续跑会报「来源组不存在」（改造前同样会报，非回归）；② 重建管道 `s2_plan_clean.py` 不产 `plan/source_groups.csv`，`s4` 只能回退读 `source/`。两处都不影响当前功能，登记待办。
- [x] **`get_group_config()` 的 `rules` 键已改「列 + 表」合并（2026-11-05 收口）**：此前它只解析 `source_groups.rules` 列，任何调用方拿 `config["rules"]` 喂解析都会静默丢掉表里的教学规则。现由 `source_group_rules_service.rules_for_parsing()` 转发，反向依赖用函数内延迟 import（理由见 spec D9）；空配置仍返回 `None` 保持旧契约。
- [x] **「列 + 表」合并逻辑已收口到一处（2026-11-05）**：`_merged_rules(db, sg, *, strict)` 是唯一实现，`_serialize_rules`（展示，坏 JSON 原样回显）与 `rules_for_parsing`（解析，坏 JSON 422）只在错误策略上分叉，合并口径不再各写一遍。
- [x] 资产标签系统设计方案（`tags` + `asset_item_tags`，项级多值，ADR-0019）
- [x] 数据库设计优化（记录级 EAV → 项级列 + 五张受控字典，ADR-0019；跨库重建见本地 spec 的 P2/P3）

- [ ] **凭证管理的收尾项（evidence-manager 票外发现，2026-11-05）**：
  - [ ] 迁移 `0030_drop_evidence_anchor` 无专属用例（`backend/tests/test_migration_0030.py` 未建）：当前只有副本库演练证据；守卫分支（表不存在 / 无该列 / 缺父表 / `foreign_keys` 为 ON / 索引重建 / 二跑 noop）无自动化覆盖，后续改迁移 runner 时不能立刻验。
  - [x] `/records?evidence_id=` 与「显示历史（含被更正）」无联动：`view=ledger` 只切视图，`include_superseded` 由勾选框单独控制，故下钻后条数仍可能少于凭证的 `used_rows`。**已实现（2026-11-05，`.scratch/records-evidence-history/`）**：带 `evidence_id` 进入时默认展开历史（生效值 = 手动覆盖 ?? URL 有 `evidence_id`），手动取消有效、清除筛选不复位；运行库实测 4 张凭证的差距（id=8 为 12 → 13）已对齐 `used_rows`。
  - [ ] 凭证列表「已使用」徽章按 `used_rows`（资产行数），删除守卫按四类引用（资产/审计/本金变更/转账事件）：若后三类对某凭证非零而资产行为 0，会出现「显示未使用但删除 409」。运行库三处当前为 0，前端以 toast 兜底。
  - [ ] `screenshot_evidence.supplement_rows` 与 `engine` 两个**只写不读**字段的存废另议（spec §4 明确本轮不动）。
  - [ ] `ParsedScreenshot.total` 删列后已无读者（仅 `screenshot_cli.py` 打印），连同残留文案一并清理：`screenshot_cli.py:41` 仍打印「投顾总金额(锚点)」；`frontend/src/lib/api.ts:898`、`screenshot/page.tsx:312` 注释仍提 `anchor=null`。
  - [ ] `deploy/build/` 打包产物仍是删列前的代码，**部署前必须重打包**；`data/rebuild/*.csv` 表头仍含 `anchor_amount`（数据工件，下次重建会重新生成，无需手改）。

- [x] **凭证命名的收尾项（evidence-naming 票外发现，2026-11-05）**（四项均已闭环）：
  - [x] **`/preview-jobs` 未支持 `recognition` 字段**（**已做，2026-11-06**）：给 job 的 payload 与 `run_preview_job` 加了同款可选字段（`preview-job-recognition` 票 01），续跑工厂一并从 payload 取回。但**首次** AI 预览提交时手里仍没有识别行（`img.aiOnly` 要等轮询后 `runAiRecognition` 才产生），故「只预览不采纳」的首次会话仍记 `items:null` —— 已由 `PATCH /evidence/{id}/recognition` 回写端点补齐（`evidence-recognition-writeback` 票 01）。
  - [x] 运行库孤儿文件 1 个（68 字节假图，库内按路径与 sha 均零引用）：2026-11-06 查证后已备份删除；磁盘 29 → 28，与 28 行一一对应。仓库本地旧库配对的 `data/evidence` 另有 2 个，也不是运行库的东西。
  - [x] 仓库本地 `data/evidence/` 的 68 字节测试残留（**已修，2026-11-06**）：根因是 `tests/test_peer_engine_modes.py` 的 `call_preview()` 不走 `call_apply` 的 `_evidence_root` patch，落进仓库默认目录。已在模块 autouse fixture 里把存证根钉到 `tmp_path`，并删掉 11 个历史残留（两库均零引用：本地 `data/person_fin.db` 与 `deploy/data/person_fin.db`）。
  - [x] 同 sha 重复行与唯一索引（**已做，2026-11-06**）：查重收窄到「同一来源组」+ 迁移 `0033` 删存量重复行 + 复合唯一索引 `ux_screenshot_evidence_sha_group`（`evidence-dedup-index`，ADR-0024）。仍然**不做**的：合并「一图属多组」的关联表——跨组同图现在各存一份、各带自己的 `sg<组id>`，表达上是正确的。运行库待停服窗口执行 0033。


### 低优先级

- [ ] `deploy/scripts/release.sh:78` 的 `DIRTY` 判定在 sudo 下可疑：2026-11-10 13:40 那次台账记了 `dirty-tree`，但发布前后 `git status --porcelain` 均只有台账自身一行。建议把 `git status` 输出打到发布日志，便于事后判断（源：`.scratch/asset-record-edit/report.md` 偏离 16）
- [ ] `records/page.tsx` 的 `['assetItems']` 与 `['itemNotes']` 两个 queryKey 可合并：票 14 后两者都来自 `GET /api/asset-items`，页面级因此白发一次请求。源：`.scratch/asset-record-edit/issues/14-asset-item-note-read.md`
- [ ] `scripts/ci.sh` 的前端类型检查改 `bunx tsc --noEmit --incremental false`：走 tsconfig 的 `incremental: true` 会漏检跨文件全局类型增强（2026-11-10 实测 `tsc` 报 0 错误而 `next build` 在 `PeriodPicker.tsx:70` 报错）。源：`.scratch/asset-record-edit/issues/12-remove-period-entrypoints-frontend.md`
- [ ] `NOTE_TOO_LONG` / `ASSET_ITEM_NOT_FOUND` 未登记进 `backend/errors.py::ErrorCode`（票 09 以字面量传 `ApiError`，当时 `errors.py` 不在写域）；补枚举并回归错误码契约（源：`.scratch/asset-record-edit/issues/09-asset-item-note-endpoint.md`）
- [ ] dev 前端端口 6679 落在 WHATWG Fetch bad-port 清单内（`6665-6669`/`6679`/`6697`），Firefox/Chrome 拒连 → `bash scripts/dev-up.sh` 起来后浏览器打不开 `/records`。应急：`socat TCP-LISTEN:6680,fork TCP:127.0.0.1:6679` 透明转发，访问 `http://localhost:6680`；正解是改 dev 前端端口或用 Firefox `network.security.ports.banned.override=6679`（源：`.scratch/asset-record-edit/issues/01-tag-note-ui-placement.md`）
- [ ] `GET /assets/last-snapshot` 的前端方法 `getLastSnapshot`（`frontend/src/lib/api.ts:219`）无任何消费方，是死方法——确认后删端点或补消费方（源：`.scratch/asset-record-edit/issues/02-period-feature-deletion-inventory.md`）

- [x] 资产配置默认是“偏离超过±5%时会给出调整建议”，需要实现添加自定义阈值的功能（`allocation_targets.alert_threshold`＋0017 双阈值/相对阈值）
- [x] 添加新表，用于记录多成员姓名；资产记录添加资产所属成员的字段；
- [ ] 资产记录批量复制上一期添加计算器功能，方便直接计算合并多个项目的金额。
- [ ] **审计 reason 转 API 必填**（L2，源：`.scratch/audit-best-practice/report.md` 遗留）——改完之后，删资产/改归属/改分类/归档/恢复这类「纠正」动作在界面上**必须**选原因（可选态 → 强制态）。**进度（2026-11-07）**：可选通道已通（`.scratch/audit-reason-capture/spec.md` 冻结并落地，票面 R3 明确「不做必填」），6 个用户端点接受 `?reason=&reason_note=`、前端三处输入已上线、`KNOWN_DEBT` 10 → 7；**本项剩余**＝把可选转强制（无理由即 422）、给 7 条无用户入口的债务补入口或明确豁免、ADR-0027 D7 修订。业主裁决：先做可选、暂缓强制。
- [x] **`/api/backup` 删除把 404 吞成 500**（L1，源：票 02 收尾发现）——已修（`b969caf`，spec `.scratch/backup-delete-404/spec.md` 冻结落地）：`routers/backup.py` 在 `except Exception` 之前加 `except (HTTPException, ApiError): raise`，删不存在的备份文件得 404 且零审计行；`RuntimeError`（删文件失败）仍 500。`routers/imports.py` 的同类端点核查后无需改（无兜底）。
- [x] **`INVALID_CURSOR` 进 `errors.ErrorCode` 枚举**（L0/L1，源：票 02 实现偏离 5）——已做：`ErrorCode.INVALID_CURSOR`（400）成为单一真源，`asset_history_service` 改用它；字面值不变，前端匹配无影响。
- [ ] **运维：`llm_providers` 的「百炼 Qwen」密钥为空**（需业主提供，非代码）——**业主 2026-11-07 裁决：保持现状（留空、`enabled=0`），不再追问**。若日后恢复，用 `scripts/restore_llm_provider_key.py` 从备份包取回（默认 dry-run）。
- [ ] `docs/agents/issue-tracker.md` 的票状态写法与校验口径不一致：文中写 `Status:` 行，而 `scripts/check-doc-driven.sh` 的 C5 要求 `- **Status:** 值`（冒号在加粗内、ASCII）；全角冒号会被判「缺 Status 行」。有人顺手改过这份文档，按「票外不改」已回退；下次动它时一并订正。

- [x] **`/api/analysis/mixed-chart` 日粒度 730 期 N+1 查询（5.5s）**（L1/L2，源：`.scratch/analysis-detail-prune/report.md` §6 二轮实测）——**已解决（2026-11-08，`.scratch/chart-table-window-perf` 票 03）**：`get_total_trend` 改为 `_latest_records_by_as_of` 一次取数 + 内存扫掠，day 粒度 SQL 条数从 `1+账期数` 降到恒定 3 条；730 期中位 149ms（旧 7064ms），端点 0.63–0.73s，五档逐字段对账差异行数 0。复现：`bash scripts/verify_analysis_ui.sh --days 730`。

- [ ] **`mixed-chart` 的极端规模仍随「活跃项数 × 账期数」增长**（L2，源：`.scratch/chart-table-window-perf/report.md` §4 D4）——合成 133 活跃项 × 730 日（约 97k 行）时单请求约 2.4s，而真库现状（133 项 / 67 个快照日）只要 46–60ms。这不是逐期查库回来了（SQL 仍恒定 3 条），是内存扫掠与记录集规模成正比。可选方向：按身份预排序后一次性双指针推进（省掉每期每身份的 `bisect_right`）、或对 day 结果按「链头 + 最新快照日」做短 TTL 缓存。**暂不处理**：现役数据规模离这条上限还有两个数量级。

- [ ] **补 `CONTEXT.md` 词条：截图原图查看**（L0，源：`.scratch/image-viewer/report.md` §五.3）——功能为纯 UI 交互、无新领域词条，落地时 `CONTEXT.md` 正被并发会话改动，为避免把在途内容混入提交而按 `Doc-Waiver` 跳过；待该文件收口后补一句「原图查看（evidence 端点 + blob 兜底，查看器不改数据）」。

- [ ] **`assets.updated_at` 排序缺索引**（L2，源：`.scratch/records-updated-at/spec.md` §4）——记录明细默认按它排序，现为全表排序后取前 N；表规模超过 5 万行或排序明显变慢时补索引，因走 `backend/migrations/` 需同时新增 ADR（校验 C3 强制）。
- [ ] **记录明细新增最右列后，1536/1600 视口把操作列滚出屏幕**（L1，源：`records-updated-at` 票 02 量测）——可用宽 1248/1312、列宽合计 1364，横向滚动 118/54px，滚出视口的恰是编辑/删除操作列。可行方向：操作列 `sticky right`，或把信息列在更宽断点以下折叠。本次业主裁决「接受滚动」。
- [ ] **`formatDateTime` 不处理超过 3 位的小数秒**（L1，源：`records-updated-at` 票 02）——后端 `format_date()` 走 `datetime.isoformat()` 给出 6 位小数秒，Safari 历史版本解析失败时 `formatDateTime` 回落原串；`formatRelativeTime` 已有「裁到毫秒」先例，可同法收口，影响 12 处调用点。
- [ ] **开发库 schema 落后迁移两步**（L1，源：`.scratch/db-dev-suffix` 票 01 实测）——搬迁后的 `data/person_fin_dev.db` 为 `schema_version=36`，而迁移已到 `0038`；下次开发后端启动会自动补跑（`main.py` lifespan `run_migrations()`），属预期行为，但需在授权窗口内做一次并核对行数/审计链，别与改名搬迁混在同一步。

- [ ] **C 路：让合并前的贵金属子项重新独立计入**（L2，源：`.scratch/archived-ingest-scope/evidence/2026-11-10_archived-scope-impact.md`）——业主 2026-11-10 已定「A+B+C 三路并进」，A/B 已随 `archived-ingest-scope` 落地；C 属数据侧，未开工。两条路径：**C1 保链只翻 status**（`set_item_status` 清 `archived_effective_from`，现成脚本 `scripts/archive_item.py --unarchive`，可逆）代价是 10 个读出口双算、`get_item_trend(family=True)` 族合计三算；**C2 破触发器删链**与「链不可变」冲突且不可回退。需先出只读影响面再逐次授权执行，并配独立 ADR。
- [ ] **`get_returns` 的归档缺口**（L1，源：`.scratch/archived-ingest-scope/spec.md` §4）——该出口取收益时未按归档生效日剔除，与 `_sum_as_of` 不同源；另立票修。
- [ ] **关联弹窗对归档项走 N+1**（L1，源：`.scratch/archived-ingest-scope/report.md` §6）——`/lineage-links/family` 无批量参数，前端按项并发取归档生效日与 merge 去向；归档项多时弹窗会变慢，可考虑给 family 加批量参数或让 `GET /api/asset-items` 行形状直接带 `archived_effective_from`。
- [ ] **`GET /api/asset-items` 行形状缺 `archived_effective_from`**（L1，源同上）——前端只能绕血缘端点拼归档日期；补字段可一并解掉上一条的 N+1。
- [ ] **同步预览管线缺 aggregate 提示**（L1，源：`.scratch/archived-ingest-scope/report.md` §4-D6）——异步 `_run_pipeline` 会给汇总行挂提示，同步版没有，属既有不对称；本次只对齐了 `archived_merged_into`。
- [ ] **现役 dev 库没有 draft 项**（L1，源：`.scratch/archived-ingest-scope/report.md` §6）——「draft 亦可 bind」这条口径在真实数据上无法覆盖验证，只在临时副本造过。可在开发库建一个分类未补齐的项作长期样本，或用 fixture 固化。

## 已完成功能

- \[x]

***

**使用说明**：

- 在相应优先级下添加新功能
- 使用 `- [ ]` 表示待完成
- 使用 `- [x]` 表示已完成
- 可随时添加、修改或删除条目


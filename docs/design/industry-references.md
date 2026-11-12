# 业界参考调研（资产配置 / 收益算法 / AI 建议 / 市场数据）

> 用途：设计阶段的外部参考输入（来源以权威标准与一手文档为主），供业务与系统架构引用；不构成对采用的承诺。

## 1. 资产配置方法论与参数化

**可建模的核心参数三元组（Vanguard 正式定义）**：再平衡策略 = **frequency（监控频率）+ threshold（偏离阈值）+ destination（回调目标：完全回目标 / 回阈值边缘 / 回中点）**。这三项即配置对象的 schema 骨架。另有**目标占比向量**、**风险偏好等级**（决定目标组合模板选择）。

- Vanguard 结论：**不存在最优阈值/频率**（年化 +5% 绝对带与月度 +0% 带风险调整收益几乎无差），差异主要是成本 → 系统应允许多策略并存并记录选择理由，而非内置「最优算法」；对多数股债组合推荐「年度/半年监控 + 5% 绝对带」。
- 阈值需支持**双形态**：绝对偏离（±5pp）与**相对偏离**（目标 10% 的 ±25% 相对带）。Kitces/Daryanani 研究：多持仓时绝对带对小组合失效，**相对带 20%** 附近最优；常见「5/25 规则」= 绝对 5pp 或相对 25% 取先触发者。偏离分析引擎应同时输出两种偏离度。
- **全天候/风险平价**（Bridgewater）：概念是「按 增长↑↓ × 通胀↑↓ 四象限各配 25% **风险预算**」，本质是风险贡献均等而非资金均等，需波动率数据。个人系统可降级为「资金占比 × 资产波动率估算」的近似，不必实现协方差优化。
- **Glide path（目标日期型）**：目标占比是**时间/距目标日年限的函数** w(t)（分段线性即可），偏离分析必须对「当期目标」而非静态目标计算 → 目标配置需支持 schedule/曲线字段。
- **标普家庭资产四象限**：实为保险公司营销概念，无权威原始文献、无实证支持 → 只可作为「用途分桶」（要花的钱 / 保钱的 / 生钱的钱 / 圆梦钱）的**预置模板**，不具方法论地位。

来源：[Vanguard Rational Rebalancing](https://www.vanguardsouthamerica.com/content/dam/intl/americas/documents/latam/en/2022/10/mx-sa-2558523-rational-rebalancing-an-analytical-approach.pdf)、[Vanguard Getting Back on Track](https://www.vanguardmexico.com/content/dam/intl/americas/documents/mexico/en/mx-en-pf-getting-back-on-track.pdf)、[Kitces 容差带](https://www.kitces.com/blog/best-opportunistic-rebalancing-frequency-time-horizons-vs-tolerance-band-thresholds/)、[Bridgewater 全天候自述](https://www.cmgwealth.com/wp-content/uploads/2015/10/Our-Thoughts-about-Risk-Parity-and-All-Weather-Bridgewater-Ray-Dalio-2015.pdf)、[Vanguard 再平衡入门](https://investor.vanguard.com/investor-resources-education/portfolio-management/rebalancing-your-portfolio)

## 2. 收益算法与数据契约（区分本金与收益的关键）

**分工**：TWR 衡量「策略本身赚了多少」（剔除现金流择时），XIRR/MWR 衡量「你的钱实际体验如何」（含择时）。复盘「建议质量」用 TWR，复盘「自己操作」用 XIRR；两者并排展示可归因到出入金择时。

- **TWR 最小契约**：①带时间戳的**组合估值序列**（净值快照）；②标记过的**外部现金流**。GIPS 规则：2005 后须按日加权现金流（**Modified Dietz** 即合格近似，只需期初/期末值 + 每笔现金流金额/日期/占期天数权重）；2010 后须对「大额现金流」当日重估值——**「大额」阈值需前瞻定义**（个人系统可取组合的 10-20%）。子区间收益**几何链接**。
- **现金流分类是本金/收益分离的根**：GIPS 定义——资产内生收益（股息/利息）**不算外部现金流**，除非离开组合。→ 交易模型需区分：外部注入/提取（本金）、内部收益（分红再投 vs 出金语义不同）、买卖（内部转移）。累积收益 = 现值 − Σ净本金，直接可算；XIRR 仅在需要年化率时用。
- **XIRR 契约**（Excel 事实标准）：输入 `[(date, signed_amount)]`，365 天贴现，**必须至少一正一负**；期初值记负、出入金按投资者视角定号、期末市值记正（一笔，勿与逐笔卖出重复计）；**迭代求解可能不收敛** → API 需返回收敛状态与 guess 参数，UI 需展示「率不可得」降级为绝对收益。
- **工程教训**：估值序列是硬前提——只有流水无快照就只能退化为「简单收益」；应自部署之日起**按行情日落净值快照表**（write-once），复盘才可重放。

来源：[GIPS 计算方法指南](https://www.gipsstandards.org/wp-content/uploads/2021/03/calculation_methodology_gs_2011.pdf)、[Microsoft XIRR](https://support.microsoft.com/en-us/excel/xirr-function)、[AnalystPrep CFA L3](https://analystprep.com/study-notes/cfa-level-iii/fundamentals-of-compliance/)、[LongsPeak TWR vs MWR](https://www.longspeakadvisory.com/blog/when-to-use-time-weighted-return-twr-vs-money-weighted-return-mwr)

## 3. AI 建议系统架构模式（生成→门→人审→复盘）

共识拓扑：**LLM 生成 → 确定性 guardrail 门 → 人工审批 → 执行/落库**，全程写审计。与「只建议不自动执行」原则完全同构，关键在**结构性隔离**而非约定：

- **建议与账本物理隔离**：AI 路径的终点只能是 `recommendation` 表（pending 态）；账本写入是**独立的人工触发交易**，AI 侧代码不存在该路径——「违规建议无法送达」应由结构保证（Wren 称 synthesizer 在 gate fail 边**不可达**并有测试锁定）。
- **持久化人审中断**：LangGraph `interrupt_before` + DB checkpoint 模式（alphagen）：审批暂停的图状态存库，**重启后可续**、拒绝/超时也落入终态（pending/approved/rejected/applied/expired 状态机），不留僵尸。适合单人系统的「异步采纳」。
- **LLM 不做算术**：风险分、偏离度、收益率一律**确定性代码计算**，LLM 只做叙述与假设生成；建议须带**可解析引用**（每条断言引用的数据点必须在证据集内存在，机器校验）——同时防幻觉并锚定「建议时点信息集」，是复盘的前提。可加独立 critic（最好不同模型家族）预审，但裁定权在人。
- **审计追溯**：append-only（进阶：hash-chain）日志，记录输入快照（当时的净值/行情/参数）、模型与 prompt 版本、门裁决、人裁决及理由。复盘 = 建议时点快照 vs 事后实际（含「未采纳的反事实」对比）。监管风向（FINRA 2025/26 报告）点名「无人工验证的自主 agent」为风险、要求 recordkeeping——个人系统按此对齐即可。

来源：[30 Agents #26 金融顾问 agent](https://neurals.ca/30-agents/financial-advisory/)、[alphagen（FastAPI+LangGraph 人审门）](https://github.com/surajkmr4218/alphagen)、[Wren（gate+hash 链审计）](https://github.com/leighanne77/Wren/blob/main/README.md)、[AI PB 生产系统（论文）](https://arxiv.org/html/2510.20099v1)

## 4. 市场数据集成：显式 API vs LLM 联网检索

**边界原则**：一切**进入计算的数字**（净值、汇率、波动率）必须来自结构化 provider 快照（带 source + timestamp + 精度标记）；**LLM 联网检索只服务叙事**（新闻、观点、逻辑），其内嵌数字视为未验证，不得回填计算。

- **Ghostfolio 是最佳本地自托管参照**（开源）：统一 `DataProviderInterface`（canHandle/getQuotes/getHistorical/getAssetProfile）+ 多实现（Yahoo/CoinGecko/Alpha Vantage/**MANUAL 手动价**）；查询走「缓存 → DB 历史表 → 外部 API 兜底」分层，历史数据以本地表为主 → **外部 API 失效不破坏复盘**。MANUAL 源对非标/境内资产尤其适用（配合截图录入流）。
- 报价须带**状态标签**：real-time/delayed/手工、「never execution-grade」；AI 建议引用行情时只读带标签快照，不现拉网络。
- **缺数据路径要显式设计**：Ghostfolio issue #6482 教训——缺汇率致 `undefined` 传染使整个收益计算崩溃；需定义 carry-forward / 报错 / 跳过三选一并在 UI 标注。
- 抓取节奏：定时任务落快照（write-once、幂等 upsert），设请求超时与并发上限；个人系统单 provider + 缓存通常已够，接口抽象留换源余地。

来源：[ghostfolio data-provider.service.ts](https://github.com/ghostfolio/ghostfolio/blob/main/apps/api/src/services/data-provider/data-provider.service.ts)、[Ghostfolio 数据提供者架构综述](https://deepwiki.com/zx2592/myportfolio/3.2-data-providers)、[Ghostfolio #6482](https://github.com/ghostfolio/ghostfolio/issues/6482)

*注：整理自设计阶段调研任务，来源含权威标准、开源仓库、论文与实务博客，引用时按需回溯原文。*
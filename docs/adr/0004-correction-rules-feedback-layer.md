# 规则反哺层：截图噪声校正按来源组存为可复用规则（子串匹配、手动勾选取）

截图录入的校正反馈复用现有 `SourceGroup.rules`（JSON，已透传经 `get_group_config` → `organize_blocks`），扩展 `rename[]` / `ignore[]` 两键，在 `_run_pipeline` 的 `parse_screenshot` 之后、`compute_preview` 之前以纯函数 `apply_correction_rules` 应用。

**决策**：噪声校正规则按**来源组**作用域（不同 App 噪声隔离）；**手动勾选「存为规则」** 才持久化（避免一次性误判断固化）；**子串匹配**（来源名包含即命中，对固定截图集最稳）。

**Considered Options**：
- 新建 `source_group_rules` 表（更规范、可逐条启用/列表管理）→ 弃：项目既有模式是「规则 = 来源组上的 JSON」（`SourceGroup.rules` 已是解析 rules 的落点），复用之零迁移、seam 一致，且校正规则量小，JSON 足够。
- 规则作用于 `organize_blocks` 块级 ignore_words / 重构布局 → 弃：当前噪声是**名称级**拆断/误识，校正应在名称层 post-parse，不是块组织层；不改通用解析器核心。
- 全局作用域 → 弃：不同 App 噪声相互污染。
- 每次自动学 → 弃：会存进一次性误判断。
- 正则/精确匹配 → 弃：维护成本高 / 逐条繁琐，子串够用。

**Consequences**：`apply_correction_rules` 为纯函数（单测友好）；preview 与 apply 共用 `_run_pipeline` 故同时生效；前端「存为规则」调 correction-rules 端点把修正落回该组 rules JSON。规则叠加在通用解析器之上，不改其核心算法。后续若规则量增长，可再拆独立表，本 ADR 不排斥。

# 资产/负债以「正数金额 + side 字段」表达（弃用负号）

`assets` 增加 `side` 字段（`asset` / `liability`）区分资产与负债，金额恒为正；负债不再用负号表达。净资产 = 资产金额之和 − 负债金额之和。

**Considered Options**：曾用「负号区分」（负数即负债）。负号把账户类别硬塞进金额符号，属单式记账的简写；复式记账里金额恒正、方向由账户类别决定。改用正数 + side 更贴合会计语义，也为未来引入流水/复式（交易有方向、金额恒正）铺路。

**Consequences**：需要一趟迁移（加 `side` 列、按符号回填、金额取绝对值），并把散落在 proportion / analysis / alert / allocation / import 各服务里的 `amount > 0` / `amount < 0` 判断改为按 `side` 判定。
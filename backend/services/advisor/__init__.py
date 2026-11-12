"""建议闭环包（三期-3）：只暴露只读端口、信息集冻结与确定性门。

隔离纪律（ADR-0013 D2/D3）：本包**不 import 任何账本写服务/账本模型**，
只依赖 `ports.ReadModel` / `ports.LlmGateway` 两个 Protocol；账本实体一律以
文本 `source_ref` 引用。W2/W3 无路由、无账本写入。
"""
from services.advisor.ports import LlmGateway, ReadModel

__all__ = ["ReadModel", "LlmGateway"]
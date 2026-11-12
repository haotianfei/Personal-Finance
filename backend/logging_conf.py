"""结构化日志配置（structlog ProcessorFormatter 官方模式）。

- 控制台：LOG_FORMAT=console 时彩色人读（非 tty 自动降级纯文本）；LOG_FORMAT=json 时逐行 JSON
- 文件：logs/backend.log 恒为 JSON，按天轮转保留 14 天
- uvicorn 日志汇入 root，同一管道渲染
"""

import logging
import os
import sys

import structlog


def setup_logging(level: str | None = None, log_dir: str | None = None) -> None:
    level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    fmt = os.getenv("LOG_FORMAT", "console")  # console | json
    log_dir = log_dir or os.getenv("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    pre_chain = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=pre_chain + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    console_renderer = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # 保证 LOG_FORMAT=json 时 stdout 也带完整 traceback（ConsoleRenderer 对
            # exception 键有专门的可读渲染，console 模式不受损）
            structlog.processors.format_exc_info,
            console_renderer,
        ],
        foreign_pre_chain=pre_chain,
    )
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)

    from logging.handlers import TimedRotatingFileHandler

    file_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        foreign_pre_chain=pre_chain,
    )
    file_handler = TimedRotatingFileHandler(
        os.path.join(log_dir, "backend.log"),
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(console_handler)
    root.addHandler(file_handler)
    root.setLevel(level)

    # uvicorn 日志汇入同一管道：清掉其自带 handler，让其 propagate
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    # 降噪
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if level == "DEBUG" else logging.WARNING
    )

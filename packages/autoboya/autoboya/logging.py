from __future__ import annotations

import logging as std_logging
import re
from pathlib import Path

from .config import LOG_FILE
from .storage import AutoBoyaStore


def redact(text: object) -> str:
    value = str(text)
    value = re.sub(r"(password=)[^\s&]+", r"\1<redacted>", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(Authtoken|auth_token|token|ticket|loginName|phone|sessionId|CASTGC|JSESSIONID|ak|sk)[\"']?[ =:]+[\"']?[^\s;,\"'&<>]+", r"\1=<redacted>", value, flags=re.IGNORECASE)
    value = re.sub(r"user \[[^\]]+\]", "user [REDACTED]", value)
    value = re.sub(r"\b\d{8,10}\b", "<student-id>", value)
    return value


class RedactingFilter(std_logging.Filter):
    def filter(self, record: std_logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def mask_username(username: str) -> str:
    if len(username) <= 3:
        return "***"
    return username[:3] + "***"


def configure_logging(store: AutoBoyaStore | None = None, verbose: bool = False, console: bool = True) -> None:
    store = store or AutoBoyaStore()
    store.init()
    log_path: Path = store.path(LOG_FILE)
    level = std_logging.DEBUG if verbose else std_logging.INFO
    handlers: list[std_logging.Handler] = [std_logging.FileHandler(log_path, encoding="utf-8")]
    if console:
        handlers.append(std_logging.StreamHandler())
    for handler in handlers:
        handler.addFilter(RedactingFilter())
    std_logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def log_event(logger: std_logging.Logger, level: int, message: str, **fields: object) -> None:
    suffix = " ".join(f"{key}={redact(value)}" for key, value in fields.items())
    logger.log(level, "%s %s", redact(message), suffix)

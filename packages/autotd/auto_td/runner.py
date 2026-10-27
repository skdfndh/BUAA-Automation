import logging
import random
import time
from typing import Callable, Iterable, Optional

from .client import TDClient
from .limits import has_reached_td_limit, td_limit_message
from .models import BatchResult, User, UserResult
from .storage import AppStorage
from .telemetry import enqueue_snapshot_event


LOGGER = logging.getLogger("auto_td")
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False

Sleeper = Callable[[int], None]


def run_user_pipeline(
    storage: AppStorage,
    client: TDClient,
    user: User,
    sleeper: Sleeper = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    raise RuntimeError("旧批处理入口已关闭，请使用持久化调度器，防止重复提交")


def run_all_users(
    storage: AppStorage,
    client: Optional[TDClient] = None,
    users: Optional[Iterable[User]] = None,
    sleeper: Sleeper = None,
    logger: Optional[logging.Logger] = None,
) -> BatchResult:
    from .scheduler import run_scheduler_once

    if users is not None:
        raise ValueError("请在持久化配置中选择用户")
    return run_scheduler_once(storage, client=client, logger=logger)


def sleep_with_countdown(seconds: int) -> None:
    if seconds <= 0:
        return
    for _ in range(seconds):
        time.sleep(1)


def _record_count(storage: AppStorage, user: User, count: Optional[int], count_source: str = "observation") -> None:
    if count is not None:
        storage.set_last_count(user.student_id, count, count_source=count_source)

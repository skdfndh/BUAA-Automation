from .models import User
from .storage import AppStorage


TD_COMPLETION_LIMIT = 32


def has_reached_td_limit(storage: AppStorage, user: User) -> bool:
    count = storage.get_last_count(user.student_id)
    limit = storage.load_settings().get("integration", {}).get("target_count", TD_COMPLETION_LIMIT)
    return count is not None and count >= limit


def td_limit_message(user: User) -> str:
    return "本学期 TD 次数已达目标，跳过后续入口打卡"

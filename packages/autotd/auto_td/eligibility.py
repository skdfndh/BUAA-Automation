from datetime import date, datetime, time, timedelta

from .constants import BEIJING_TZ, DEFAULT_WINDOWS


def waiting_reason(storage, user, user_state: dict, now: datetime) -> str | None:
    settings = storage.load_settings().get("integration", {})
    if settings.get("enabled") is not True:
        return "TD 自动化未启用"
    if not settings.get("term_id"):
        return "缺少当前学期标识"
    try:
        storage.validate_user(user)
    except (OSError, ValueError) as exc:
        return f"图片或机位配置无效，尚未提交：{exc}"
    start = settings.get("start_date")
    if not start or now.date() < date.fromisoformat(start):
        return f"尚未到启用日期（{start or '未配置'}）"
    end = settings.get("end_date")
    if end and now.date() > date.fromisoformat(end):
        return "已超过配置的结束日期"
    if not 1 <= user.rounds <= 3 or user.wait_time_min < 180 or user.wait_time_max < user.wait_time_min:
        return "轮数或等待时间配置无效"
    target = settings.get("target_count", 32)
    if type(target) is not int or target <= 0:
        return "目标次数无效"
    if storage.get_last_count(user.student_id) is None:
        return "缺少当前学期可信次数，等待人工确认"
    for day, entry in storage.load_state().get("daily_runs", {}).items():
        for previous in entry.get("users", {}).values():
            if day < str(now.date()) and (previous.get("status") == "error" or
                    (previous.get("next_action") == "exit" and previous.get("status") != "completed")):
                return "历史任务存在未确认结果，等待人工核验"
    if user_state.get("status") == "completed" or user_state.get("next_action") == "exit":
        return None
    if user_state.get("status") == "error":
        return None
    timeout = float(storage.load_config().get("server", {}).get("timeout", 10))
    budget = user.wait_time_max + 4 * timeout + 90
    for window in DEFAULT_WINDOWS:
        start_text, end_text = window.split("-")
        beginning = datetime.combine(now.date(), time.fromisoformat(start_text), BEIJING_TZ)
        ending = datetime.combine(now.date(), time.fromisoformat(end_text), BEIJING_TZ)
        if beginning <= now < ending and now + timedelta(seconds=budget) >= ending:
            return "当前窗口剩余时间不足以完成入口和出口"
    return None

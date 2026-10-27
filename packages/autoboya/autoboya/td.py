from __future__ import annotations

import logging
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cache import CourseCache
from .config import ICLASS_CACHE_FILE, RUN_LOCK_FILE, RUN_PID_FILE, SETTINGS_FILE, STOP_FILE
from .logging import mask_username, redact
from .rules import parse_dt
from .storage import AutoBoyaStore

BEIJING_TZ = timezone(timedelta(hours=8))


def td_store(store: AutoBoyaStore) -> AutoBoyaStore:
    return AutoBoyaStore(store.path("td"))


def td_running(store: AutoBoyaStore) -> bool:
    from .status import worker_running

    return worker_running(td_store(store))


def parent_running(store: AutoBoyaStore) -> bool:
    from .status import worker_running

    return worker_running(store)


def redact_td_text(storage, value, users=None):
    value = redact(value)
    if users is None:
        try:
            users = storage.list_users()
        except (OSError, ValueError, KeyError, TypeError):
            users = []
    for user in users:
        value = value.replace(user.student_id, "<student-id>").replace(user.card_id, "<card-id>")
    return value


def ensure_td_worker(store: AutoBoyaStore) -> None:
    if store.load_json(SETTINGS_FILE, {}).get("td_enabled") is not True or td_running(store):
        return
    # 启动锁避免多个管理入口同时派生子进程；运行锁由子进程持有。
    with store.lock("run/td-start.lock"):
        if td_running(store):
            return
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                   "env": {**os.environ, "AUTOBOYA_HOME": str(store.root.resolve())},
                   "cwd": str(Path(__file__).resolve().parent.parent)}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        subprocess.Popen([sys.executable, "-m", "autoboya", "td-worker"], **options)


def collect_td_status(store: AutoBoyaStore) -> dict:
    result = {"enabled": False, "running": False, "heartbeat": "暂无", "state": "未配置", "message": "", "users": [],
              "settings_unreadable": True, "stopping": store.path(STOP_FILE).exists()}
    try:
        settings = store.load_json(SETTINGS_FILE, {})
        if not isinstance(settings, dict):
            raise ValueError("自动化设置格式不正确")
        result["enabled"] = settings.get("td_enabled") is True
        result["settings_unreadable"] = False
        home = td_store(store)
        result["running"] = td_running(store)
        health = home.load_json("health.json", {})
        for key in ("heartbeat", "state", "message"):
            if key in health:
                result[key] = redact(health[key])
        if not home.path("users.json").exists():
            return result
        from auto_td.storage import AppStorage

        storage = AppStorage(home.root)
        policy = storage.load_settings().get("integration", {})
        result["target_count"] = policy.get("target_count", 32)
        day = str(datetime.now(BEIJING_TZ).date())
        states = storage.load_state().get("daily_runs", {}).get(day, {}).get("users", {})
        users = storage.list_users()
        for user in users:
            state = states.get(user.student_id, {})
            result["users"].append({"account": mask_username(user.student_id), "count": storage.get_last_count(user.student_id),
                                    "rounds": user.rounds, "completed_rounds": state.get("completed_rounds", 0),
                                    "status": state.get("status", "pending"), "phase": state.get("phase", "idle"),
                                    "last_error": redact_td_text(storage, state.get("last_error") or "", users)})
        result["message"] = redact_td_text(storage, result["message"], users)
    except (OSError, ValueError, TypeError, ImportError, KeyError) as exc:
        result.update(state="error", message=redact(exc))
    return result


def check_td_alerts(store, now=None):
    now = now or datetime.now(BEIJING_TZ)
    report = collect_td_status(store)
    issues = []
    if report.get("settings_unreadable"):
        issues.append({"kind": "settings", "message": "自动化配置无法读取，不能确认是否启用：" + report["message"]})
    if report["enabled"]:
        heartbeat = None
        if report["heartbeat"] != "暂无":
            try:
                heartbeat = datetime.fromisoformat(report["heartbeat"])
                if heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=BEIJING_TZ)
            except ValueError:
                issues.append({"kind": "heartbeat", "message": "TD 心跳时间格式错误，无法判断运行是否及时"})
        configured = store.path("td/settings.json")
        grace = configured.exists() and (now.timestamp() - configured.stat().st_mtime < 120)
        stopping = report.get("stopping", False)
        if not parent_running(store) and not grace and not stopping:
            issues.append({"kind": "parent", "message": "TD 已启用但博雅父后台未运行，请检查开机任务"})
        if not report["running"] and not grace and not stopping:
            issues.append({"kind": "process", "message": "TD 已启用但独立后台未运行，请检查父后台和开机任务"})
        elif heartbeat and (now - heartbeat).total_seconds() > 300 and not stopping:
            issues.append({"kind": "heartbeat", "message": "TD 超过 5 分钟未更新心跳，请检查日志和进程"})
        if report["state"] in {"error", "blocked"} or any(text in report["message"] for text in ("缺少当前学期", "历史任务存在未确认", "次数回退")):
            issues.append({"kind": "execution", "message": report["message"]})
        for user in report["users"]:
            if user["status"] == "error":
                issues.append({"kind": "action", "account": user["account"], "phase": user["phase"], "message": user["last_error"]})
    old = store.load_json("td/notification_state.json", {})
    previous = set(old.get("active", []))
    current = {}
    for issue in issues:
        fingerprint = hashlib.sha256(json.dumps(issue, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        current[fingerprint] = issue
    if set(current) != previous:
        store.save_json("td/notification_state.json", {"active": sorted(current), "checked_at": now.isoformat(timespec="seconds")})
    return [issue for fingerprint, issue in current.items() if fingerprint not in previous]


def conflict_reason(store, now, seconds):
    end = now + timedelta(seconds=seconds)
    for courses in CourseCache(store).load_selected().values():
        for raw in courses:
            cfg = raw.get("courseSignConfig") or {}
            if isinstance(cfg, str):
                import json
                cfg = json.loads(cfg)
            windows = [(raw.get("courseStartDate"), raw.get("courseEndDate")),
                       (cfg.get("signStartDate"), cfg.get("signEndDate")),
                       (cfg.get("signOutStartDate"), cfg.get("signOutEndDate"))]
            for start, finish in windows:
                a, b = parse_dt(start), parse_dt(finish)
                if a and b and a.replace(tzinfo=BEIJING_TZ) <= end and b.replace(tzinfo=BEIJING_TZ) >= now:
                    return "与已选博雅课程或签到窗口重叠，等待空闲时段"
    classes = store.load_json(ICLASS_CACHE_FILE, {})
    active = [u.username for u in store.user_records() if u.enabled]
    for username in active:
        entry = classes.get(username, {})
        updated = datetime.fromisoformat(entry["updated_at"]) if entry.get("updated_at") else None
        if not updated or updated.replace(tzinfo=BEIJING_TZ) < now - timedelta(minutes=10):
            return "等待普通课程日程成功刷新后再启动 TD 入口"
        for course in entry.get("courses", []):
            a, b = parse_dt(course.get("begin")), parse_dt(course.get("end"))
            if a and b and a.replace(tzinfo=BEIJING_TZ) <= end and b.replace(tzinfo=BEIJING_TZ) >= now:
                return "当前时段与普通课程重叠，等待空闲时段"
    return None


def td_tick(store, storage, now=None, client=None, network_probe=None):
    from auto_td.client import TDClient
    from auto_td.eligibility import waiting_reason
    from auto_td.scheduler import in_any_window, run_scheduler_once
    from auto_td.constants import DEFAULT_WINDOWS

    now = now or datetime.now(BEIJING_TZ)
    users = storage.list_users()
    active = {u.username for u in store.user_records() if u.enabled}
    if not users or any(user.student_id not in active for user in users):
        return "blocked", "TD 账号必须与现有已启用账号匹配"
    state = storage.load_state()
    states = state.get("daily_runs", {}).get(str(now.date()), {}).get("users", {})
    pending = []
    for user in users:
        current = states.get(user.student_id, {})
        reason = waiting_reason(storage, user, current, now)
        if reason:
            return ("blocked" if any(word in reason for word in ("无效", "缺少", "历史任务")) else "waiting"), reason
        if current.get("status") == "error":
            return "error", current.get("last_error") or "TD 存在未确认结果，需人工核验"
        due = datetime.fromisoformat(current["due_at"]) if current.get("due_at") else now
        if (current.get("status") != "completed" and due <= now and
                (current.get("next_action") == "exit" or storage.get_last_count(user.student_id) < storage.load_settings()["integration"].get("target_count", 32))):
            pending.append((user, current))
    if pending and in_any_window(now, DEFAULT_WINDOWS):
        if all(current.get("next_action") != "exit" for _, current in pending):
            reason = conflict_reason(store, now, max(user.wait_time_max for user, _ in pending) + 90)
            if reason:
                return "waiting", reason
        config = storage.load_config()
        probe = network_probe or socket.create_connection
        try:
            with probe((config["server"]["ip"], int(config["server"]["port"])), timeout=2):
                pass
        except OSError:
            return "blocked", "TD 服务器不可达，尚未提交打卡；等待网络恢复"
    run_scheduler_once(storage, now=now, client=client or TDClient(storage.load_config()))
    states = storage.load_state().get("daily_runs", {}).get(str(now.date()), {}).get("users", {})
    errors = [s.get("last_error") for s in states.values() if s.get("status") == "error"]
    if errors:
        return "error", str(errors[0])
    if states and all(s.get("status") == "completed" for s in states.values()):
        return "completed", "今日轮次已完成或累计已达目标，不再提交入口"
    return "waiting", "按合法时间窗口和已保存进度运行，等待下一动作"


def run_td_worker(store):
    from auto_td.storage import AppStorage
    from auto_td.logging_utils import setup_logging

    home = td_store(store)
    storage = AppStorage(home.root)
    with home.lock(RUN_LOCK_FILE):
        pid = home.path(RUN_PID_FILE)
        pid.write_text(str(os.getpid()), encoding="utf-8")
        logger = setup_logging(storage, stream=False)
        previous = None
        try:
            while not store.path(STOP_FILE).exists() and store.load_json(SETTINGS_FILE, {}).get("td_enabled") is True:
                try:
                    state, message = td_tick(store, storage)
                except Exception as exc:
                    state, message = "error", redact(exc)
                health = {"heartbeat": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
                          "state": state, "message": redact_td_text(storage, message)}
                home.save_json("health.json", health)
                if (state, message) != previous:
                    logger.log(logging.ERROR if state in {"error", "blocked"} else logging.INFO, "%s: %s", state, redact(message))
                    previous = (state, message)
                for _ in range(15):
                    if store.path(STOP_FILE).exists() or store.load_json(SETTINGS_FILE, {}).get("td_enabled") is not True:
                        return
                    time.sleep(1)
        finally:
            pid.unlink(missing_ok=True)

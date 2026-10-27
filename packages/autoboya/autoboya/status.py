from __future__ import annotations

import errno
import os
import re
import subprocess
from datetime import datetime

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from .config import ACTION_JOURNAL_FILE, COURSE_CACHE_FILE, ICLASS_CACHE_FILE, LOG_FILE, RUN_LOCK_FILE, RUN_PID_FILE, SETTINGS_FILE
from .logging import mask_username, redact
from .rules import auto_select_categories
from .storage import AutoBoyaStore

LOG_LIMIT = 2 * 1024 * 1024
LOG_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+\s+(\w+)\s+(.*)$")
ACTION_PATTERN = re.compile(
    r"automation action user=(\S+) action=(select|sign|signout|class_sign) course_id=(\S+) ok=(True|False) reason=(.*)"
)
ACTION_NAMES = {"select": "博雅选课", "sign": "博雅签到", "signout": "博雅签退", "class_sign": "课堂签到"}


def worker_running(store: AutoBoyaStore) -> bool:
    # PID 文件可能因崩溃残留，实际运行状态以操作系统互斥锁为准。
    try:
        with store.path(RUN_LOCK_FILE).open("rb") as handle:
            if os.name == "nt":
                import msvcrt

                lock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                lock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                lock()
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                return True
            unlock()
    except FileNotFoundError:
        pass
    return False


def task_state() -> str:
    if os.name != "nt":
        return "非 Windows 环境"
    command = (
        "$ErrorActionPreference='Stop';"
        "$t=Get-ScheduledTask -TaskName 'AutoBoya-AutoStart' -ErrorAction SilentlyContinue;"
        "if($null -eq $t){'NotFound'}else{$t.State.ToString()}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW, check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "查询失败，请在任务计划程序中检查"
    state = result.stdout.strip()
    return {"Running": "运行中", "Ready": "就绪，当前未运行", "Disabled": "已禁用", "NotFound": "未找到自动启动任务"}.get(state, state or "无法确认")


def recent_log_lines(store: AutoBoyaStore) -> list[str]:
    with store.path(LOG_FILE).open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        start = max(0, handle.tell() - LOG_LIMIT)
        handle.seek(start)
        data = handle.read(LOG_LIMIT)
    if start:
        data = data.partition(b"\n")[2]
    return data.decode("utf-8", errors="replace").splitlines()


def collect_status(store: AutoBoyaStore, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    report = {"now": now, "running": None, "pid": "未记录", "heartbeat": "暂无", "checks": None,
              "outcomes": [], "errors": [], "issues": [], "course_names": {}, "class_course_names": {},
              "class_signin_enabled": False, "class_heartbeat": "暂无", "class_checks": None, "class_courses": [],
              "course_cache_updated": "暂无", "course_count": 0}
    try:
        report["running"] = worker_running(store)
        if store.path(RUN_PID_FILE).exists():
            report["pid"] = store.path(RUN_PID_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        report["issues"].append(f"读取后台状态失败：{redact(exc)}")
    try:
        lines = recent_log_lines(store)
    except FileNotFoundError:
        lines = []
    except OSError as exc:
        lines = []
        report["issues"].append(f"读取日志失败：{redact(exc)}")
    for line in lines:
        match = LOG_PATTERN.match(redact(line))
        if not match:
            continue
        when, level, message = match.groups()
        if "automation heartbeat" in message:
            report["heartbeat"] = when
            count = re.search(r"auto_boya_check=(\d+)", message)
            report["checks"] = int(count.group(1)) if count else None
        if "classroom heartbeat" in message:
            report["class_heartbeat"] = when
            count = re.search(r"auto_class_check=(\d+)", message)
            report["class_checks"] = int(count.group(1)) if count else None
        action = ACTION_PATTERN.fullmatch(message)
        if action:
            user, kind, course_id, ok, reason = action.groups()
            report["outcomes"].append((when, user, kind, course_id, "成功" if ok == "True" else "失败", reason))
        if level in {"WARNING", "ERROR", "CRITICAL"}:
            report["errors"].append((when, message))

    for relative in (COURSE_CACHE_FILE, ACTION_JOURNAL_FILE, SETTINGS_FILE, ICLASS_CACHE_FILE):
        try:
            raw = store.load_json(relative, [] if relative == COURSE_CACHE_FILE else {})
            if relative == COURSE_CACHE_FILE:
                if not isinstance(raw, list):
                    raise ValueError("课程缓存格式不正确")
                report["course_names"] = {str(item.get("id")): str(item.get("courseName") or item.get("name") or "")
                                          for item in raw if isinstance(item, dict)}
                report["course_count"] = len(raw)
                path = store.path(COURSE_CACHE_FILE)
                if path.exists():
                    updated = datetime.fromtimestamp(path.stat().st_mtime)
                    report["course_cache_updated"] = updated.strftime("%Y-%m-%d %H:%M:%S")
                    if report["running"] and (now - updated).total_seconds() > 900:
                        report["issues"].append("课程列表已超过 15 分钟未完整更新，请检查分页超时或网络错误；现有签到仍按缓存处理。")
            elif relative == ACTION_JOURNAL_FILE:
                if not isinstance(raw, dict):
                    raise ValueError("动作记录格式不正确")
                add_journal_outcomes(report["outcomes"], raw)
                for key, value in raw.items():
                    if ":select_failed:" in key and isinstance(value, dict) and value.get("disabled"):
                        user, course_id = key.split(":select_failed:", 1)
                        report["issues"].append(f"{mask_username(user)} 的课程 {course_id} 已停止自动选课重试：{redact(value.get('message', ''))}")
            elif relative == SETTINGS_FILE:
                if not isinstance(raw, dict):
                    raise ValueError("设置格式不正确")
                report["class_signin_enabled"] = raw.get("class_signin_enabled") is True
                report["auto_select_categories"] = sorted(auto_select_categories(raw) - {"其他方面"})
            else:
                if not isinstance(raw, dict):
                    raise ValueError("普通课程缓存格式不正确")
                for user, entry in raw.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("courses"), list):
                        continue
                    for course in entry["courses"]:
                        if not isinstance(course, dict):
                            continue
                        report["class_course_names"][str(course.get("id"))] = str(course.get("name") or "")
                        if entry.get("date") == str(now.date()):
                            report["class_courses"].append((mask_username(user), str(course.get("name") or ""),
                                str(course.get("begin") or ""), str(course.get("end") or ""), course.get("sign_status")))
        except (OSError, ValueError, TypeError) as exc:
            report["issues"].append(f"读取 {relative} 失败：{redact(exc)}")
    report["outcomes"] = sorted(report["outcomes"], reverse=True)[:10]
    report["errors"] = list(reversed(report["errors"][-8:]))
    if report["heartbeat"] != "暂无" and report["running"]:
        try:
            heartbeat = datetime.strptime(report["heartbeat"], "%Y-%m-%d %H:%M:%S")
            if (now - heartbeat).total_seconds() > 300:
                report["issues"].append("后台仍占有运行锁，但超过 5 分钟没有新心跳，请检查最近错误。")
        except ValueError:
            report["issues"].append("最近心跳的时间格式不正确，无法判断是否及时。")
    if report["class_signin_enabled"] and report["running"] and report["class_heartbeat"] != "暂无":
        try:
            if (now - datetime.strptime(report["class_heartbeat"], "%Y-%m-%d %H:%M:%S")).total_seconds() > 300:
                report["issues"].append("普通课程签到超过 5 分钟没有成功扫描，请检查最近错误。")
        except ValueError:
            report["issues"].append("普通课程签到心跳时间格式不正确。")
    return report


def add_journal_outcomes(outcomes: list, journal: dict) -> None:
    seen = {(row[0][:10], row[1], row[2], row[3]) for row in outcomes}
    for key, value in journal.items():
        parts = key.split(":", 3)
        if len(parts) != 4 or parts[2] not in ACTION_NAMES or not isinstance(value, str):
            continue
        date, user, action, course_id = parts
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        masked = mask_username(user)
        if (date, masked, action, course_id) not in seen:
            outcomes.append((date, masked, action, course_id, "成功", redact(value)))


def show_status(store: AutoBoyaStore, console: Console, now: datetime | None = None) -> None:
    report = collect_status(store, now)
    state = {True: "运行中", False: "未运行", None: "无法确认"}[report["running"]]
    console.print("AutoBoya 后台状态", style="bold cyan")
    console.print(f"查看时间：{report['now']:%Y-%m-%d %H:%M:%S}")
    console.print(f"后台：{state}    PID：{report['pid']}", markup=False)
    console.print(f"开机自动启动任务：{task_state()}", markup=False)
    console.print(f"博雅课程列表：{report['course_count']} 门    最近完整更新：{report['course_cache_updated']}")
    console.print(f"最近心跳：{report['heartbeat']}")
    categories = "、".join(report.get("auto_select_categories", [])) or "未勾选或无法读取，暂停新选课"
    console.print(f"博雅自动选课类别：{categories}", markup=False)
    if report["checks"] == 0:
        console.print("博雅心跳报告：当前没有课程处于自动签到／签退窗口。")
    elif report["checks"] is not None:
        console.print(f"博雅心跳报告：有 {report['checks']} 门课程处于签到／签退窗口，结果见下表。")
    console.print(f"普通课程自动签到：{'已启用' if report['class_signin_enabled'] else '已停用'}    最近成功扫描：{report['class_heartbeat']}")
    from .td import collect_td_status

    td = collect_td_status(store)
    if td["enabled"] or td["users"]:
        console.print(f"TD：{'运行中' if td['running'] else '未运行'} / {td['state']}    心跳：{td['heartbeat']}", markup=False)
        console.print(f"TD 说明：{td['message']}", markup=False)
        for user in td["users"]:
            console.print(f"TD {user['account']}：累计 {user['count']}/{td.get('target_count', 32)}，"
                          f"今日 {user['completed_rounds']}/{user['rounds']} 轮，阶段 {user['phase']}，{user['last_error']}", markup=False)
    if report["class_checks"] is not None:
        console.print(f"普通课程最近扫描待处理课堂：{report['class_checks']} 门。")
    if report["class_courses"]:
        classes = Table(title="今日普通课程（最近查询状态）", expand=True)
        for name in ("账号", "课程", "上课时间", "签到状态"):
            classes.add_column(name, overflow="fold")
        for user, name, begin, end, sign_status in report["class_courses"]:
            classes.add_row(*(escape(str(value)) for value in
                              (user, name, f"{begin} - {end}", {0: "未签到", 1: "已签到"}.get(sign_status, "未知"))))
        console.print(classes)
    table = Table(title="最近自动操作结果（最多 10 项）", expand=True)
    for name in ("时间", "账号", "动作", "课程", "结果", "说明"):
        table.add_column(name, overflow="fold")
    for when, user, action, course_id, ok, reason in report["outcomes"]:
        names = report["class_course_names"] if action == "class_sign" else report["course_names"]
        course_name = names.get(course_id, "")
        table.add_row(*(escape(str(value)) for value in
                        (when, user, ACTION_NAMES[action], f"{course_id} {course_name}", ok, reason)))
    console.print(table)
    if not report["outcomes"]:
        console.print("暂无后台操作记录。已选课程会跳过选课，签到和签退会等待对应时间窗口。")
    console.print("最近错误与告警（最近日志范围，最多 8 条）", style="bold yellow")
    for when, message in report["errors"]:
        console.print(f"{when}  {message}", markup=False)
    if not report["errors"]:
        console.print("最近日志范围内未发现错误或告警。")
    for issue in report["issues"]:
        console.print(f"提示：{issue}", style="yellow", markup=False)
    console.print("成功表示接口已确认；仅有日期的成功记录来自历史动作记录，未记录具体时分秒。")
    console.print(f"日志文件：{store.path(LOG_FILE)}", markup=False)


def view_status(store: AutoBoyaStore, interactive: bool = False) -> None:
    console = Console()
    while True:
        show_status(store, console)
        if not interactive:
            break
        try:
            choice = input("\n按回车刷新，输入 Q 后回车退出：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if choice == "q":
            break
        console.clear()

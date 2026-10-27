from __future__ import annotations

import os
import time
import logging
from dataclasses import asdict
from datetime import datetime
from typing import Callable, Iterable

from .cache import CourseCache
from .config import ACTION_JOURNAL_FILE, ICLASS_CACHE_FILE, RUN_LOCK_FILE, RUN_PID_FILE, SETTINGS_FILE, STOP_FILE
from .auth import serialize_cookies
from .iclass import IclassClient, IclassCourse, class_signin_candidate
from .exceptions import AlreadySelected, BoyaApiError, CourseFull, CourseNotOpen, InteractiveLoginRequired, SelectionLimitReached, SessionExpired
from .logging import log_event, mask_username
from .models import ActionResult, AutomationDecision, BoyaCourse, UserRecord
from .rules import current_semester_window, has_autonomous_sign, is_auto_select_candidate, random_point_in_radius, sign_window_for
from .session import ensure_bykc_client, force_login_bykc_client, close_client
from .storage import AutoBoyaStore
from .filters import load_auto_select_categories

logger = logging.getLogger(__name__)
SELECT_FAILURE_LIMIT = 3


def decide_actions(
    courses: Iterable[BoyaCourse],
    selected_by_user: dict[str, list[BoyaCourse]],
    now: datetime | None = None,
    categories: frozenset[str] | None = None,
) -> list[AutomationDecision]:
    now = now or datetime.now()
    decisions: list[AutomationDecision] = []
    for username, selected_courses in selected_by_user.items():
        for course in selected_courses:
            if not has_autonomous_sign(course):
                continue
            if sign_window_for(course, 1, now):
                decisions.append(AutomationDecision(action="sign", course_id=course.id, username=username))
            if sign_window_for(course, 2, now):
                decisions.append(AutomationDecision(action="signout", course_id=course.id, username=username))
    # 签到窗口可能较短，优先于批量选课执行。
    decisions.extend(
        AutomationDecision(action="select", course_id=course.id)
        for course in courses
        if is_auto_select_candidate(course, now, ignore_selected=True, categories=categories)
    )
    return decisions


class AutomationRunner:
    def __init__(self, store: AutoBoyaStore) -> None:
        self.store = store
        self.cache = CourseCache(store)
        self._login_required: dict[str, int | None] = {}
        self._unpersisted_actions: dict[str, str] = {}
        self._iclass_clients: dict[str, tuple[int | None, IclassClient]] = {}
        self._last_signin_service = float("-inf")

    def run_forever(self) -> None:
        self.store.init()
        with self.store.lock(RUN_LOCK_FILE):
            try:
                self._run_forever()
            finally:
                self.close_class_clients()

    def _run_forever(self) -> None:
        stop = self.store.path(STOP_FILE)
        if stop.exists():
            stop.unlink()
        self.store.path(RUN_PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
        last_refresh = float("-inf")
        try:
            while not stop.exists():
                decisions: list[AutomationDecision] = []
                try:
                    decisions = self.scan_once()
                    self.log_heartbeat(decisions)
                    self.execute_decisions(decisions)
                except Exception as exc:
                    log_event(logger, logging.ERROR, "automation loop failed", error=exc)
                try:
                    self.run_class_signins_once()
                except Exception as exc:
                    log_event(logger, logging.ERROR, "classroom loop failed", error=exc)
                try:
                    from .td import ensure_td_worker
                    ensure_td_worker(self.store)
                except Exception as exc:
                    log_event(logger, logging.ERROR, "TD worker start failed", error=exc)
                now = time.monotonic()
                self._last_signin_service = now
                if now - last_refresh >= 60:
                    last_refresh = now
                    try:
                        self.refresh_once(after_page=self._service_due_signins)
                        if stop.exists():
                            break
                        # 先处理已有窗口，再检查刷新后新增的动作，避免网络请求拖延已有签到。
                        attempted = set(decisions)
                        self.execute_decisions([item for item in self.scan_once() if item not in attempted])
                    except Exception as exc:
                        log_event(logger, logging.ERROR, "automation refresh failed", error=exc)
                for _ in range(60):
                    if stop.exists():
                        break
                    time.sleep(1)
        finally:
            pid = self.store.path(RUN_PID_FILE)
            if pid.exists():
                pid.unlink()

    def request_stop(self) -> None:
        self.store.init()
        self.store.path(STOP_FILE).write_text(datetime.now().isoformat(), encoding="utf-8")

    def _service_due_signins(self) -> None:
        if self.store.path(STOP_FILE).exists():
            raise InterruptedError("已请求停止，取消剩余课程分页")
        now = time.monotonic()
        if now - self._last_signin_service < 15:
            return
        self._last_signin_service = now
        try:
            active = {user.username for user in self.store.user_records() if user.enabled}
            selected = {user: courses for user, courses in self._selected_by_user(self.cache.load_selected()).items()
                        if user in active}
            decisions = decide_actions([], selected, datetime.now())
            self.log_heartbeat(decisions)
            self.execute_decisions(decisions)
        except Exception as exc:
            log_event(logger, logging.ERROR, "sign scan during refresh failed", error=exc)

    def refresh_once(self, after_page: Callable[[], None] | None = None) -> None:
        users = [user for user in self.store.user_records() if user.enabled]
        if not users:
            return
        for user in users:
            started = time.monotonic()
            try:
                courses = self._with_reauth(user.username, lambda client: client.query_courses(after_page=after_page)
                                            if after_page else client.query_courses())
                self.cache.save_courses(courses)
                log_event(logger, logging.INFO, "course refresh completed", user=mask_username(user.username),
                          courses=len(courses), elapsed_seconds=round(time.monotonic() - started, 2))
                break
            except InterruptedError:
                return
            except Exception as exc:
                log_event(logger, logging.WARNING, "course refresh failed", user=mask_username(user.username), error=exc)
        for user in users:
            try:
                self._refresh_user_cache(user.username)
            except Exception as exc:
                log_event(logger, logging.WARNING, "user cache refresh failed", user=mask_username(user.username), error=exc)

    def scan_once(self) -> list[AutomationDecision]:
        courses = self.cache.parsed_courses()
        active = {user.username for user in self.store.user_records() if user.enabled}
        selected = {user: items for user, items in self._selected_by_user(self.cache.load_selected()).items() if user in active}
        return decide_actions(courses, selected, datetime.now(), categories=load_auto_select_categories(self.store))

    def run_once(self) -> list[ActionResult]:
        self.store.init()
        with self.store.lock(RUN_LOCK_FILE):
            try:
                self.refresh_once()
                return self.execute_decisions(self.scan_once()) + self.run_class_signins_once()
            finally:
                self.close_class_clients()

    def _class_client(self, username: str, bykc_client) -> IclassClient:
        path = self.store.path(f"sessions/{username}.json")
        modified = path.stat().st_mtime_ns if path.exists() else None
        cached = self._iclass_clients.get(username)
        if cached is None or cached[0] != modified:
            if cached:
                cached[1].close()
            client = IclassClient(cookies=serialize_cookies(bykc_client.client))
            self._iclass_clients[username] = (modified, client)
        return self._iclass_clients[username][1]

    def close_class_clients(self) -> None:
        for _, client in self._iclass_clients.values():
            client.close()
        self._iclass_clients.clear()

    def _save_class_cache(self, username: str, courses: list[IclassCourse], now: datetime) -> None:
        cache = self.store.load_json(ICLASS_CACHE_FILE, {})
        cache[username] = {"date": str(now.date()), "updated_at": now.isoformat(timespec="seconds"),
                           "courses": [asdict(course) for course in courses]}
        self.store.save_json(ICLASS_CACHE_FILE, cache)

    def query_class_signins(self, username: str, bykc_client, now: datetime | None = None) -> list[IclassCourse]:
        now = now or datetime.now()
        courses = self._class_client(username, bykc_client).query_today(now)
        self._save_class_cache(username, courses, now)
        return courses

    def run_class_signins_once(self) -> list[ActionResult]:
        settings = self.store.load_json(SETTINGS_FILE, {})
        if settings.get("class_signin_enabled") is not True:
            return []
        results = []
        for user in self.store.user_records():
            if not user.enabled:
                continue
            try:
                results.extend(self._with_reauth(user.username, lambda client: self._sign_classes_for_user(user.username, client)))
            except Exception as exc:
                log_event(logger, logging.WARNING, "classroom scan failed", user=mask_username(user.username), error=exc)
        return results

    def _sign_classes_for_user(self, username: str, bykc_client) -> list[ActionResult]:
        now = datetime.now()
        courses = self.query_class_signins(username, bykc_client, now)
        journal = self._load_action_journal()
        pending = [course for course in courses if class_signin_candidate(course, now)
                   and not journal.get(journal_key(username, "class_sign", course.id, now))]
        log_event(logger, logging.INFO, "classroom heartbeat", user=mask_username(username),
                  courses=len(courses), auto_class_check=len(pending))
        results = []
        for course in pending:
            key = journal_key(username, "class_sign", course.id, now)
            # 同一轮列表可能重复包含一个排课，成功后仍需再检查动作记录。
            if journal.get(key):
                continue
            try:
                message = self._class_client(username, bykc_client).sign_course(course.id)
                result = ActionResult(username, "class_sign", course.id, True, message)
            except SessionExpired:
                raise
            except Exception as exc:
                result = ActionResult(username, "class_sign", course.id, False, str(exc))
            self._record_result(result, journal, key)
            results.append(result)
            if result.ok:
                course.sign_status = 1
                try:
                    self._save_class_cache(username, courses, now)
                except Exception as exc:
                    log_event(logger, logging.WARNING, "classroom cache update failed", user=mask_username(username), error=exc)
        return results

    def _load_action_journal(self) -> dict:
        journal = self.store.load_json(ACTION_JOURNAL_FILE, {})
        if self._unpersisted_actions:
            journal.update(self._unpersisted_actions)
            self.store.save_json(ACTION_JOURNAL_FILE, journal)
            self._unpersisted_actions.clear()
        return journal

    def log_heartbeat(self, decisions: list[AutomationDecision]) -> None:
        users = [user for user in self.store.user_records() if user.enabled]
        auto_check_by_user: dict[str, set[int]] = {user.username: set() for user in users}
        for decision in decisions:
            if decision.action not in {"sign", "signout"} or not decision.username:
                continue
            if decision.username in auto_check_by_user:
                auto_check_by_user[decision.username].add(decision.course_id)
        for user in users:
            log_event(
                logger,
                logging.INFO,
                "automation heartbeat",
                user=mask_username(user.username),
                auto_boya_check=len(auto_check_by_user[user.username]),
            )

    def execute_decisions(self, decisions: list[AutomationDecision]) -> list[ActionResult]:
        results: list[ActionResult] = []
        selected_raw = self.cache.load_selected()
        selected_by_user = self._selected_by_user(selected_raw)
        courses_by_id = {course.id: course for course in self.cache.parsed_courses()} if any(item.action == "select" for item in decisions) else {}
        journal = self._load_action_journal()
        users = [user for user in self.store.user_records() if user.enabled]
        active = {user.username for user in users}
        for decision in decisions:
            if decision.action == "select":
                course = courses_by_id.get(decision.course_id)
                for user in users:
                    # 窗口保存后立即读取新限制，避免执行上一轮已生成的选课动作。
                    if course is None or course.category not in load_auto_select_categories(self.store) or course.category == "其他方面":
                        continue
                    if course and not course_matches_user_campus(course, user):
                        continue
                    if user_has_selected(selected_by_user, user.username, decision.course_id):
                        continue
                    key = journal_key(user.username, "select", decision.course_id)
                    if journal.get(key):
                        continue
                    if select_failure_attempts(journal, user.username, decision.course_id) >= SELECT_FAILURE_LIMIT:
                        continue
                    result = self._select_for_user(user, decision.course_id)
                    results.append(result)
                    if result.ok:
                        journal.pop(select_failure_key(user.username, decision.course_id), None)
                    elif result.terminal:
                        attempts = record_select_failure(journal, user.username, decision.course_id, result.message)
                        log_event(
                            logger,
                            logging.WARNING,
                            "auto select failed",
                            user=mask_username(user.username),
                            course_id=decision.course_id,
                            attempts=attempts,
                            limit=SELECT_FAILURE_LIMIT,
                            disabled=attempts >= SELECT_FAILURE_LIMIT,
                            reason=result.message,
                        )
                    self._record_result(result, journal, key)
                continue
            username = decision.username
            if not username or username not in active:
                continue
            key = journal_key(username, decision.action, decision.course_id)
            if journal.get(key):
                continue
            course = next((item for item in selected_by_user.get(username, []) if item.id == decision.course_id), None)
            result = self._sign_for_user(username, course, decision.action)
            results.append(result)
            self._record_result(result, journal, key)
        return results

    def _record_result(self, result: ActionResult, journal: dict, key: str) -> None:
        if result.ok:
            journal[key] = result.message
            self._unpersisted_actions[key] = result.message
        # 每个已确认的动作先落盘，再执行任何附加查询。
        self.store.save_json(ACTION_JOURNAL_FILE, journal)
        self._unpersisted_actions.clear()
        log_event(
            logger, logging.INFO if result.ok else logging.WARNING, "automation action",
            user=mask_username(result.username), action=result.action, course_id=result.course_id,
            ok=result.ok, reason=result.message,
        )
        if result.ok and result.action != "class_sign":
            try:
                self._refresh_user_cache(result.username)
            except Exception as exc:
                log_event(logger, logging.WARNING, "action succeeded but cache refresh failed",
                          user=mask_username(result.username), action=result.action, course_id=result.course_id, error=exc)

    def _select_for_user(self, user: UserRecord, course_id: int) -> ActionResult:
        try:
            self._with_reauth(user.username, lambda client: client.select_course(course_id))
            return ActionResult(user.username, "select", course_id, True, "selected")
        except AlreadySelected:
            return ActionResult(user.username, "select", course_id, True, "already selected")
        except BoyaApiError as exc:
            return ActionResult(user.username, "select", course_id, False, str(exc), terminal=exc.status not in {"decode_error", "invalid_response"})
        except (CourseFull, CourseNotOpen, SelectionLimitReached) as exc:
            return ActionResult(user.username, "select", course_id, False, str(exc), terminal=True)
        except Exception as exc:
            return ActionResult(user.username, "select", course_id, False, str(exc))

    def _sign_for_user(self, username: str, course: BoyaCourse | None, action: str) -> ActionResult:
        if not course or not course.sign_config.get("signPointList"):
            return ActionResult(username, action, course.id if course else 0, False, "missing sign point")
        try:
            point = course.sign_config["signPointList"][-1]
            lat, lng = random_point_in_radius(float(point["lat"]), float(point["lng"]), float(point.get("radius") or 8))
            sign_type = 1 if action == "sign" else 2
            self._with_reauth(username, lambda client: client.sign_course(course.id, lat, lng, sign_type))
            return ActionResult(username, action, course.id, True, "signed")
        except Exception as exc:
            return ActionResult(username, action, course.id, False, str(exc))

    def _selected_by_user(self, selected_raw: object) -> dict[str, list[BoyaCourse]]:
        if not isinstance(selected_raw, dict):
            return {}
        from .bykc import parse_course

        return {
            username: [parse_course(item) for item in items]
            for username, items in selected_raw.items()
            if isinstance(items, list)
        }

    def _with_reauth(self, username: str, operation):
        session_path = self.store.path(f"sessions/{username}.json")
        modified = session_path.stat().st_mtime_ns if session_path.exists() else None
        if username in self._login_required:
            if self._login_required[username] == modified:
                raise InteractiveLoginRequired("需要人工验证码；请执行 autoboya login，登录成功后自动恢复")
            self._login_required.pop(username)
        client = None
        try:
            client = ensure_bykc_client(self.store, username)
            try:
                return operation(client)
            except SessionExpired:
                close_client(client)
                client = None
                client = force_login_bykc_client(self.store, username)
                return operation(client)
        except InteractiveLoginRequired:
            # 等待交互登录更新会话，避免后台反复触发验证或账号限流。
            self._login_required[username] = modified
            raise
        finally:
            close_client(client)

    def _refresh_user_cache(self, username: str) -> None:
        def refresh(client):
            start, end = current_semester_window(client.get_all_config())
            self.cache.save_selected(username, client.query_chosen_courses(start, end))
            self.cache.save_statistics(username, client.query_statistics())

        self._with_reauth(username, refresh)


def journal_key(username: str, action: str, course_id: int, when: datetime | None = None) -> str:
    when = when or datetime.now()
    return f"{when.date()}:{username}:{action}:{course_id}"


def select_failure_key(username: str, course_id: int) -> str:
    return f"{username}:select_failed:{course_id}"


def select_failure_attempts(journal: dict[str, object], username: str, course_id: int) -> int:
    value = journal.get(select_failure_key(username, course_id))
    if isinstance(value, dict):
        attempts = value.get("attempts")
        return attempts if isinstance(attempts, int) else 0
    return value if isinstance(value, int) else 0


def record_select_failure(journal: dict[str, object], username: str, course_id: int, message: str) -> int:
    attempts = select_failure_attempts(journal, username, course_id) + 1
    journal[select_failure_key(username, course_id)] = {
        "attempts": attempts,
        "message": message,
        "last_attempt": datetime.now().isoformat(timespec="seconds"),
        "disabled": attempts >= SELECT_FAILURE_LIMIT,
    }
    return attempts


def user_has_selected(selected_by_user: dict[str, list[BoyaCourse]], username: str, course_id: int) -> bool:
    return any(course.id == course_id for course in selected_by_user.get(username, []))


def course_matches_user_campus(course: BoyaCourse, user: UserRecord) -> bool:
    is_hangzhou = is_hangzhou_course(course)
    return is_hangzhou if user.campus == "杭州" else not is_hangzhou


def is_hangzhou_course(course: BoyaCourse) -> bool:
    values = [
        course.name,
        course.location,
        course.campus,
    ]
    if isinstance(course.raw, dict):
        values.extend(
            [
                course.raw.get("courseName"),
                course.raw.get("coursePosition"),
                course.raw.get("courseCampus"),
                course.raw.get("courseCampusList"),
            ]
        )
    return any("杭州" in stringify_campus_value(value) for value in values)


def stringify_campus_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)

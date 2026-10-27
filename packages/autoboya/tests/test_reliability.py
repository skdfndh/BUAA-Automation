import logging
from datetime import datetime
from unittest.mock import Mock

import httpx
import pytest

from autoboya import bykc, scheduler
from autoboya.crypto import BykcCrypto
from autoboya.models import AutomationDecision, BoyaCourse, UserRecord
from autoboya.storage import AutoBoyaStore


@pytest.fixture
def runner(tmp_path):
    store = AutoBoyaStore(tmp_path / "data")
    store.init()
    store.upsert_user(UserRecord(username="audit-user"))
    store.save_json("settings.json", {"auto_select_categories": ["美育"]})
    store.save_json("cache/courses.json", [{"id": 1001, "courseKind": "美育"}])
    return scheduler.AutomationRunner(store)


@pytest.fixture
def sign_course():
    return BoyaCourse(
        id=1001,
        category="美育",
        sign_config={"signPointList": [{"lat": 39.9, "lng": 116.3, "radius": 8}]},
    )


def run_one_loop(monkeypatch, runner):
    monkeypatch.setattr(scheduler.time, "sleep", lambda seconds: runner.request_stop())
    runner.run_forever()


def test_refresh_failure_does_not_skip_cached_actions(monkeypatch, runner):
    scan = Mock(return_value=[])
    monkeypatch.setattr(runner, "scan_once", scan)
    monkeypatch.setattr(runner, "refresh_once", Mock(side_effect=RuntimeError("刷新失败")))
    run_one_loop(monkeypatch, runner)
    scan.assert_called()


def test_cached_sign_runs_before_network_refresh(monkeypatch, runner):
    order = []
    monkeypatch.setattr(runner, "scan_once", lambda: [])
    monkeypatch.setattr(runner, "execute_decisions", lambda decisions: order.append("执行"))
    monkeypatch.setattr(runner, "refresh_once", lambda **kwargs: order.append("刷新"))
    run_one_loop(monkeypatch, runner)
    assert order.index("执行") < order.index("刷新")


def test_course_cache_refreshes_each_minute(monkeypatch, runner):
    refresh = Mock()
    ticks = iter([1000.0, 1060.0, 1120.0, 1180.0, 1240.0, 1300.0])
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 120:
            runner.request_stop()

    monkeypatch.setattr(scheduler.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(scheduler.time, "sleep", sleep)
    monkeypatch.setattr(runner, "refresh_once", refresh)
    monkeypatch.setattr(runner, "scan_once", lambda: [])
    runner.run_forever()
    assert refresh.call_count == 2


@pytest.mark.parametrize("action", ["select", "sign", "signout"])
def test_success_is_journaled_before_cache_refresh(monkeypatch, runner, sign_course, action, caplog):
    runner.cache.save_courses([sign_course])
    runner.cache.save_selected("audit-user", [] if action == "select" else [sign_course])
    calls = []

    def submit(username, operation):
        calls.append(username)
        return {"status": "0"}

    def refresh(username):
        journal = runner.store.load_json("cache/action_journal.json", {})
        assert journal.get(scheduler.journal_key(username, action, 1001))
        raise RuntimeError("统计请求超时")

    monkeypatch.setattr(runner, "_with_reauth", submit)
    monkeypatch.setattr(runner, "_refresh_user_cache", refresh)
    decision = AutomationDecision(action=action, course_id=1001, username=None if action == "select" else "audit-user")
    with caplog.at_level(logging.WARNING):
        first = runner.execute_decisions([decision])
        second = runner.execute_decisions([decision])
    assert first[0].ok
    assert second == []
    assert calls == ["audit-user"]
    assert "统计请求超时" in caplog.text


def test_sign_failure_has_user_action_course_and_reason(monkeypatch, runner, sign_course, caplog):
    runner.cache.save_selected("audit-user", [sign_course])
    monkeypatch.setattr(runner, "_with_reauth", Mock(side_effect=RuntimeError("签到窗口关闭")))
    with caplog.at_level(logging.WARNING):
        results = runner.execute_decisions([AutomationDecision("sign", 1001, "audit-user")])
    assert not results[0].ok
    assert "aud***" in caplog.text
    assert "action=sign" in caplog.text
    assert "course_id=1001" in caplog.text
    assert "签到窗口关闭" in caplog.text


def test_bad_sign_point_does_not_abort_other_courses(monkeypatch, runner, sign_course):
    bad = BoyaCourse(id=1002, sign_config={"signPointList": [{"lat": "错误"}]})
    runner.cache.save_selected("audit-user", [bad, sign_course])
    monkeypatch.setattr(runner, "_with_reauth", lambda username, operation: {"status": "0"})
    monkeypatch.setattr(runner, "_refresh_user_cache", lambda username: None)
    results = runner.execute_decisions([
        AutomationDecision("sign", 1002, "audit-user"),
        AutomationDecision("sign", 1001, "audit-user"),
    ])
    assert [result.ok for result in results] == [False, True]


@pytest.mark.parametrize("enabled", [False, None])
def test_disabled_or_removed_user_never_signs(monkeypatch, runner, sign_course, enabled):
    runner.cache.save_selected("audit-user", [sign_course])
    runner.store.save_users([] if enabled is None else [{"username": "audit-user", "enabled": enabled}])
    submit = Mock()
    monkeypatch.setattr(runner, "_with_reauth", submit)
    assert runner.execute_decisions([AutomationDecision("sign", 1001, "audit-user")]) == []
    submit.assert_not_called()


def test_bad_user_does_not_prevent_healthy_user_refresh(monkeypatch, runner, caplog):
    runner.store.upsert_user(UserRecord(username="healthy-user"))
    client = Mock()
    client.query_courses.return_value = [BoyaCourse(id=42)]
    client.get_all_config.return_value = {"data": {"semester": [{
        "semesterStartDate": "2000-01-01", "semesterEndDate": "2100-12-31",
    }]}}
    client.query_chosen_courses.return_value = [BoyaCourse(id=43)]
    client.query_statistics.return_value = {"validCount": 1}

    def call(username, operation):
        if username == "audit-user":
            raise RuntimeError("需要人工验证码")
        return operation(client)

    monkeypatch.setattr(runner, "_with_reauth", call)
    with caplog.at_level(logging.WARNING):
        runner.refresh_once()
    assert runner.cache.load_courses()[0]["id"] == 42
    assert runner.cache.load_selected("healthy-user")[0]["id"] == 43
    assert "需要人工验证码" in caplog.text


@pytest.mark.parametrize("payload", [{}, {"success": False}, [], None, {"status": None}, {"status": False}])
def test_unknown_response_is_never_success(monkeypatch, payload):
    crypto = BykcCrypto()
    monkeypatch.setattr(crypto, "decrypt_response", lambda body: payload)
    monkeypatch.setattr(bykc, "BykcCrypto", lambda: crypto)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"mock"))) as http:
        client = bykc.BykcClient("mock-token", http_client=http, use_vpn=False)
        with pytest.raises(bykc.BoyaApiError):
            client.select_course(1001)


def test_http_unauthorized_triggers_session_recovery():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(401))) as http:
        client = bykc.BykcClient("mock-token", http_client=http, use_vpn=False)
        with pytest.raises(bykc.SessionExpired):
            client.query_courses()


def test_semester_matches_today_instead_of_list_order():
    old = {"semesterStartDate": "2026-03-01", "semesterEndDate": "2026-06-21"}
    current = {"semesterStartDate": "2026-09-01", "semesterEndDate": "2027-01-31"}
    config = {"data": {"semester": [old, current]}}
    assert scheduler.current_semester_window(config, now=datetime(2026, 10, 27)) == tuple(current.values())


@pytest.mark.parametrize("config", [{}, {"data": None}, {"data": {"semester": []}}, {"data": {"semester": [{"semesterStartDate": "错误"}]}}])
def test_missing_semester_fails_without_historical_fallback(config):
    with pytest.raises(bykc.BoyaApiError):
        scheduler.current_semester_window(config)


def test_semester_date_only_includes_last_day():
    config = {"data": {"semester": [{"semesterStartDate": "2026-09-01", "semesterEndDate": "2027-01-31"}]}}
    assert scheduler.current_semester_window(config, now=datetime(2027, 1, 31, 20))[1] == "2027-01-31"


def test_overlapping_semesters_are_not_guessed():
    semester = {"semesterStartDate": "2026-09-01", "semesterEndDate": "2027-01-31"}
    with pytest.raises(bykc.BoyaApiError):
        scheduler.current_semester_window({"data": {"semester": [semester, semester]}}, now=datetime(2026, 10, 27))


def test_unknown_status_does_not_disable_selection(monkeypatch, runner):
    monkeypatch.setattr(runner, "_with_reauth", Mock(side_effect=bykc.BoyaApiError("invalid_response", "状态缺失")))
    for _ in range(4):
        runner.execute_decisions([AutomationDecision("select", 1001)])
    journal = runner.store.load_json("cache/action_journal.json")
    assert scheduler.select_failure_attempts(journal, "audit-user", 1001) == 0


def test_already_selected_is_reconciled_as_success(monkeypatch, runner):
    from autoboya.exceptions import AlreadySelected

    monkeypatch.setattr(runner, "_with_reauth", Mock(side_effect=AlreadySelected("已经选中")))
    monkeypatch.setattr(runner, "_refresh_user_cache", lambda username: None)
    assert runner.execute_decisions([AutomationDecision("select", 1001)])[0].ok


def test_sign_is_prioritized_over_selection(sign_course):
    sign_course.select_start = "2026-10-27 00:00:00"
    sign_course.select_end = "2026-10-28 00:00:00"
    sign_course.sign_config.update({"signStartDate": "2026-10-27 00:00:00", "signEndDate": "2026-10-28 00:00:00"})
    decisions = scheduler.decide_actions([sign_course], {"audit": [sign_course]}, now=datetime(2026, 10, 27, 12))
    assert [item.action for item in decisions] == ["sign", "select"]


def test_confirmed_action_is_not_resubmitted_after_disk_write_failure(monkeypatch, runner, sign_course):
    runner.cache.save_selected("audit-user", [sign_course])
    submit = Mock(return_value={"status": "0"})
    monkeypatch.setattr(runner, "_with_reauth", submit)
    monkeypatch.setattr(runner, "_refresh_user_cache", lambda username: None)
    save = runner.store.save_json
    writes = []

    def fail_first_write(relative, value):
        writes.append(relative)
        if len(writes) == 1:
            raise OSError("磁盘写入失败")
        save(relative, value)

    monkeypatch.setattr(runner.store, "save_json", fail_first_write)
    decision = AutomationDecision("sign", 1001, "audit-user")
    with pytest.raises(OSError, match="磁盘写入失败"):
        runner.execute_decisions([decision])
    assert runner.execute_decisions([decision]) == []
    submit.assert_called_once()
    assert runner.store.load_json("cache/action_journal.json")[scheduler.journal_key("audit-user", "sign", 1001)]

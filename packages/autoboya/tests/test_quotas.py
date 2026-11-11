from unittest.mock import Mock

import pytest

from autoboya.models import UserRecord, BoyaCourse, ActionResult, AutomationDecision
from autoboya.quotas import remaining_slots
from autoboya.scheduler import AutomationRunner
from autoboya.storage import AutoBoyaStore


def stats(required=2, completed=1, pending=0, ids=(), failed=0):
    return {"statistical": {"60|博雅课程": {"57|劳动教育": {
        "assessmentCount": required, "completeAssessmentCount": completed,
        "failAssessmentCount": failed,
        "undoneAssessmentCount": pending,
        "courseUserList": [{"courseInfo": {"id": cid}} for cid in ids],
    }}}}


@pytest.mark.parametrize("completed,pending,reserved,ids,expected", [
    (1, 0, [], (), 1), (2, 0, [], (), 0), (3, 0, [], (), 0),
    (1, 1, [], (), 0), (1, 0, [1], (), 0), (1, 1, [1], (1,), 0),
])
def test_counts_completed_pending_and_unconfirmed(completed, pending, reserved, ids, expected):
    assert remaining_slots(stats(completed=completed, pending=pending, ids=ids), "劳动教育", reserved) == expected


@pytest.mark.parametrize("value", [{}, None, {"statistical": []}, stats(required=True),
                                  stats(failed=None), stats(failed=-1), stats(failed=True), stats(failed="2")])
def test_missing_or_invalid_requirement_stops_selection(value):
    with pytest.raises(ValueError):
        remaining_slots(value, "劳动教育", [])


def test_reservation_blocks_more_courses_even_after_restart_and_stale_statistics(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="test-user"))
    store.save_json("settings.json", {"auto_select_categories": ["劳动教育"], "auto_select_requirements_only": True})
    runner = AutomationRunner(store)
    runner.cache.save_courses([BoyaCourse(id=cid, category="劳动教育") for cid in (1, 2)])
    submitted = Mock(return_value=ActionResult("test-user", "select", 1, True, "ok"))
    for current in (runner, AutomationRunner(store)):
        monkeypatch.setattr(current, "_with_reauth", lambda *args: stats())
        monkeypatch.setattr(current, "_select_for_user", submitted)
        monkeypatch.setattr(current, "_refresh_user_cache", lambda *args: None)
        current.execute_decisions([AutomationDecision("select", 1), AutomationDecision("select", 2)])
    assert submitted.call_count == 1


def test_statistics_query_failure_never_submits(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="test-user"))
    store.save_json("settings.json", {"auto_select_categories": ["劳动教育"], "auto_select_requirements_only": True})
    runner = AutomationRunner(store)
    runner.cache.save_courses([BoyaCourse(id=1, category="劳动教育")])
    monkeypatch.setattr(runner, "_with_reauth", Mock(side_effect=TimeoutError("test timeout")))
    submit = Mock()
    monkeypatch.setattr(runner, "_select_for_user", submit)
    runner.execute_decisions([AutomationDecision("select", 1)])
    submit.assert_not_called()


@pytest.mark.parametrize("terminal,expected", [(False, 1), (True, 2)])
def test_unknown_result_holds_slot_but_explicit_rejection_releases_it(tmp_path, monkeypatch, terminal, expected):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="test-user"))
    store.save_json("settings.json", {"auto_select_categories": ["劳动教育"], "auto_select_requirements_only": True})
    runner = AutomationRunner(store)
    runner.cache.save_courses([BoyaCourse(id=cid, category="劳动教育") for cid in (1, 2)])
    monkeypatch.setattr(runner, "_with_reauth", lambda *args: stats())
    submit = Mock(side_effect=lambda user, cid: ActionResult(user.username, "select", cid, False, "failure", terminal=terminal))
    monkeypatch.setattr(runner, "_select_for_user", submit)
    runner.execute_decisions([AutomationDecision("select", 1), AutomationDecision("select", 2)])
    assert submit.call_count == expected


@pytest.mark.parametrize("required,completed,failed,pending,expected", [
    (2, 3, 2, 0, 1), (1, 1, 2, 3, 0), (1, 1, 2, 0, 2),
    (1, 0, 2, 2, 1), (2, 1, 1, 0, 2),
])
def test_failures_deduct_once_without_clamping_negative_balance(required, completed, failed, pending, expected):
    assert remaining_slots(stats(required=required, completed=completed, failed=failed, pending=pending),
                           "劳动教育", []) == expected


def test_failed_course_reopens_category_after_previous_completion(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="test-user"))
    store.save_json("settings.json", {"auto_select_categories": ["劳动教育"], "auto_select_requirements_only": True})
    runner = AutomationRunner(store)
    runner.cache.save_courses([BoyaCourse(id=1, category="劳动教育")])
    query = Mock(return_value=stats(completed=2, failed=0))
    monkeypatch.setattr(runner, "_with_reauth", query)
    submit = Mock(return_value=ActionResult("test-user", "select", 1, True, "ok"))
    monkeypatch.setattr(runner, "_select_for_user", submit)
    monkeypatch.setattr(runner, "_refresh_user_cache", lambda *args: None)
    runner.execute_decisions([AutomationDecision("select", 1)])
    submit.assert_not_called()
    query.return_value = stats(completed=2, failed=1)
    runner.execute_decisions([AutomationDecision("select", 1)])
    submit.assert_called_once()

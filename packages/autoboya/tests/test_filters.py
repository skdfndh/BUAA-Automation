import json
from datetime import datetime
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from autoboya import scheduler, status
from autoboya.cache import CourseCache, preview_auto_select_courses
from autoboya.cli import app
from autoboya.filters import FilterWindow, load_auto_select_categories, save_auto_select_categories
from autoboya.models import ActionResult, AutomationDecision, BoyaCourse, UserRecord
from autoboya.rules import auto_select_categories, is_auto_select_candidate
from autoboya.storage import AutoBoyaStore

NOW = datetime(2026, 5, 20, 8, 30)


def course(course_id=1, category="美育"):
    return BoyaCourse(id=course_id, category=category, select_start="2000-01-01 00:00:00",
                      select_end="2099-01-01 00:00:00", current_count=1, max_count=20,
                      sign_config={"signPointList": [{"lat": 39.9, "lng": 116.3, "radius": 8}]})


@pytest.fixture
def store(tmp_path):
    result = AutoBoyaStore(tmp_path / "data")
    result.init()
    result.upsert_user(UserRecord(username="filter-user"))
    return result


@pytest.mark.parametrize("settings", [{}, None, [], {"auto_select_categories": None},
    {"auto_select_categories": "美育"}, {"auto_select_categories": ["美育", None]},
    {"auto_select_categories": ["美育", " "]}, {"auto_select_categories": {"美育": True}}])
def test_missing_or_invalid_filter_pauses_selection(settings):
    assert auto_select_categories(settings) == frozenset()
    assert not is_auto_select_candidate(course(), NOW, categories=auto_select_categories(settings))


def test_categories_match_exactly_and_do_not_allow_unknown():
    allowed = frozenset({"美育", "德育"})
    assert is_auto_select_candidate(course(), NOW, categories=allowed)
    assert is_auto_select_candidate(course(category="德育"), NOW, categories=allowed)
    for category in ("劳动教育", "未知类别", "", "美育拓展", "其他方面"):
        assert not is_auto_select_candidate(course(category=category), NOW, categories=allowed)
    assert not is_auto_select_candidate(course(), NOW, categories=frozenset())
    assert not is_auto_select_candidate(course(category="其他方面"), NOW, categories=frozenset({"其他方面"}))


def test_category_does_not_bypass_existing_conditions():
    item = course()
    allowed = frozenset({"美育"})
    item.max_count = item.current_count
    assert not is_auto_select_candidate(item, NOW, categories=allowed)
    item.max_count = 20
    item.select_end = "2000-01-02 00:00:00"
    assert not is_auto_select_candidate(item, NOW, categories=allowed)
    item.select_end = "2099-01-01 00:00:00"
    item.sign_config = {}
    assert not is_auto_select_candidate(item, NOW, categories=allowed)


def test_preview_and_scheduler_use_same_categories(store):
    cache = CourseCache(store)
    cache.save_courses([course(), course(2, "德育"), course(3, "新增类别")])
    allowed = frozenset({"美育"})
    preview = preview_auto_select_courses(cache.load_courses(), NOW, categories=allowed)
    decisions = scheduler.decide_actions(cache.parsed_courses(), {}, NOW, categories=allowed)
    assert [item.id for item in preview.candidates] == [1]
    assert preview.excluded == {2: "未勾选此课程类别", 3: "未勾选此课程类别"}
    assert decisions == [AutomationDecision("select", 1)]


def test_save_preserves_other_settings_and_survives_reopen(store):
    store.save_json("settings.json", {"class_signin_enabled": True, "auto_select_mode": "autonomous_sign_only"})
    save_auto_select_categories(store, ["美育", "德育", "美育"])
    assert load_auto_select_categories(AutoBoyaStore(store.root)) == frozenset({"美育", "德育"})
    settings = store.load_json("settings.json")
    assert settings["class_signin_enabled"] is True
    assert settings["auto_select_mode"] == "autonomous_sign_only"
    assert len(settings["auto_select_categories"]) == 2
    save_auto_select_categories(store, [])
    assert load_auto_select_categories(store) == frozenset()


@pytest.mark.parametrize("categories", ["美育", None, ["美育", 2], [""]])
def test_invalid_save_leaves_settings_unchanged(store, categories):
    before = store.path("settings.json").read_bytes()
    with pytest.raises(ValueError):
        save_auto_select_categories(store, categories)
    assert store.path("settings.json").read_bytes() == before


@pytest.mark.parametrize("contents", ["{broken", "[]"])
def test_bad_settings_pause_selection_and_are_not_overwritten(store, contents):
    store.path("settings.json").write_text(contents, encoding="utf-8")
    assert not load_auto_select_categories(store)
    with pytest.raises(ValueError):
        save_auto_select_categories(store, ["美育"])
    assert store.path("settings.json").read_text(encoding="utf-8") == contents


def test_scan_pauses_selection_but_keeps_sign_and_signout(store, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls):
            return NOW

    monkeypatch.setattr(scheduler, "datetime", Clock)
    runner = scheduler.AutomationRunner(store)
    item = course()
    item.sign_config.update({"signStartDate": "2026-05-20 08:00:00", "signEndDate": "2026-05-20 09:00:00",
                            "signOutStartDate": "2026-05-20 08:00:00", "signOutEndDate": "2026-05-20 09:00:00"})
    runner.cache.save_courses([item, course(2, "德育")])
    runner.cache.save_selected("filter-user", [item])
    assert [decision.action for decision in runner.scan_once()] == ["sign", "signout"]
    monkeypatch.setattr(runner, "_sign_for_user", lambda user, item, action: ActionResult(user, action, item.id, True, "ok"))
    monkeypatch.setattr(runner, "_refresh_user_cache", lambda user: None)
    assert [result.action for result in runner.execute_decisions(runner.scan_once())] == ["sign", "signout"]
    save_auto_select_categories(store, ["德育"])
    assert runner.scan_once()[-1] == AutomationDecision("select", 2)


def test_execute_rechecks_filters_after_scan(store, monkeypatch):
    runner = scheduler.AutomationRunner(store)
    runner.cache.save_courses([course()])
    save_auto_select_categories(store, ["美育"])
    decisions = runner.scan_once()
    assert decisions == [AutomationDecision("select", 1)]
    submit = Mock()
    monkeypatch.setattr(runner, "_select_for_user", submit)
    save_auto_select_categories(store, ["德育"])
    assert runner.execute_decisions(decisions) == []
    submit.assert_not_called()
    save_auto_select_categories(store, ["美育"])
    assert runner.execute_decisions([AutomationDecision("select", 99)]) == []
    submit.assert_not_called()


def test_execute_reloads_filter_between_users(store, monkeypatch):
    store.upsert_user(UserRecord(username="second-user"))
    runner = scheduler.AutomationRunner(store)
    runner.cache.save_courses([course()])
    save_auto_select_categories(store, ["美育"])
    calls = []

    def submit(user, course_id):
        calls.append(user.username)
        save_auto_select_categories(store, [])
        return ActionResult(user.username, "select", course_id, True, "ok")

    monkeypatch.setattr(runner, "_select_for_user", submit)
    monkeypatch.setattr(runner, "_refresh_user_cache", lambda user: None)
    runner.execute_decisions([AutomationDecision("select", 1)])
    assert calls == ["filter-user"]


def test_cli_preview_and_status_read_saved_filter(store, monkeypatch):
    monkeypatch.setenv("AUTOBOYA_HOME", str(store.root))
    CourseCache(store).save_courses([course(), course(2, "德育")])
    save_auto_select_categories(store, ["美育"])
    result = CliRunner().invoke(app, ["courses", "auto-preview", "--json"])
    assert result.exit_code == 0, result.output
    assert [item["id"] for item in json.loads(result.output)["candidates"]] == [1]
    assert status.collect_status(store)["auto_select_categories"] == ["美育"]
    save_auto_select_categories(store, [])
    result = CliRunner().invoke(app, ["courses", "auto-preview", "--json"])
    assert json.loads(result.output)["candidates"] == []


def test_filters_cli_opens_window_for_current_store(store, monkeypatch):
    monkeypatch.setenv("AUTOBOYA_HOME", str(store.root))
    opened = []
    monkeypatch.setattr("autoboya.filters.show_filter_window", lambda value: opened.append(value.root))
    result = CliRunner().invoke(app, ["courses", "filters"])
    assert result.exit_code == 0, result.output
    assert opened == [store.root]


def test_checkbox_window_saves_only_checked_categories(store):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("当前环境没有可用的图形显示")
    root.withdraw()
    try:
        CourseCache(store).save_courses([course(), course(2, "德育"), course(3, "其他方面")])
        window = FilterWindow(root, store)
        window.set_all(True)
        assert not window.variables["其他方面"].get()
        window.variables["德育"].set(False)
        window.save()
        assert load_auto_select_categories(store) == frozenset({"美育"})
        assert "保存成功" in window.message.get()
        window.set_all(False)
        window.save()
        assert not load_auto_select_categories(store)
        assert "暂停新选课" in window.message.get()
    finally:
        root.destroy()

import logging
from datetime import datetime
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from autoboya import cli
from autoboya.config import ACTION_JOURNAL_FILE, ICLASS_CACHE_FILE, SETTINGS_FILE
from autoboya.iclass import IclassApiError, IclassCourse
from autoboya.models import UserRecord
from autoboya.scheduler import AutomationRunner, journal_key
from autoboya.storage import AutoBoyaStore


@pytest.fixture
def setup_runner(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="audit-user"))
    fake_client = Mock()
    fake_client.query_today.side_effect = lambda now: [IclassCourse("class-1", "测试课程", "00:00", "23:59:59", 0)]
    fake_client.sign_course.return_value = "签到成功"

    def create():
        runner = AutomationRunner(store)
        monkeypatch.setattr(runner, "_class_client", lambda username, bykc: fake_client)
        monkeypatch.setattr(runner, "_with_reauth", lambda username, operation: operation(Mock()))
        return runner

    return store, fake_client, create


def test_class_automation_is_off_until_enabled(setup_runner):
    store, client, create = setup_runner
    assert create().run_class_signins_once() == []
    client.query_today.assert_not_called()
    client.sign_course.assert_not_called()


def test_success_persists_across_runner_restart_and_skips_other_system_refresh(setup_runner, monkeypatch):
    store, client, create = setup_runner
    store.save_json(SETTINGS_FILE, {"class_signin_enabled": True})
    runner = create()
    refresh = Mock()
    monkeypatch.setattr(runner, "_refresh_user_cache", refresh)
    assert runner.run_class_signins_once()[0].ok
    assert create().run_class_signins_once() == []
    client.sign_course.assert_called_once_with("class-1")
    refresh.assert_not_called()
    assert store.load_json(ACTION_JOURNAL_FILE)[journal_key("audit-user", "class_sign", "class-1")]


def test_class_query_alone_never_submits_sign(setup_runner):
    store, client, create = setup_runner
    courses = create().query_class_signins("audit-user", Mock())
    assert courses[0].name == "测试课程"
    assert store.load_json(ICLASS_CACHE_FILE)["audit-user"]["date"] == str(datetime.now().date())
    client.sign_course.assert_not_called()


def test_failure_is_visible_and_not_saved_as_success(setup_runner, caplog):
    store, client, create = setup_runner
    store.save_json(SETTINGS_FILE, {"class_signin_enabled": True})
    client.sign_course.side_effect = IclassApiError("当前还未开放签到")
    with caplog.at_level(logging.WARNING):
        result = create().run_class_signins_once()[0]
    assert not result.ok
    assert "action=class_sign" in caplog.text
    assert "当前还未开放签到" in caplog.text
    assert not store.load_json(ACTION_JOURNAL_FILE).get(journal_key("audit-user", "class_sign", "class-1"))


def test_disk_failure_does_not_repeat_acknowledged_submission(setup_runner, monkeypatch):
    store, client, create = setup_runner
    store.save_json(SETTINGS_FILE, {"class_signin_enabled": True})
    runner = create()
    save = store.save_json
    failed = []

    def save_json(relative, value, **kwargs):
        if relative == ACTION_JOURNAL_FILE and not failed:
            failed.append(True)
            raise OSError("模拟磁盘写入失败")
        save(relative, value, **kwargs)

    monkeypatch.setattr(store, "save_json", save_json)
    runner.run_class_signins_once()
    runner.run_class_signins_once()
    client.sign_course.assert_called_once()
    assert store.load_json(ACTION_JOURNAL_FILE)[journal_key("audit-user", "class_sign", "class-1")]


def test_class_cache_failure_after_success_is_not_a_sign_failure(setup_runner, monkeypatch, caplog):
    store, client, create = setup_runner
    store.save_json(SETTINGS_FILE, {"class_signin_enabled": True})
    runner = create()
    save = runner._save_class_cache
    writes = []

    def save_cache(username, courses, now):
        writes.append(True)
        if len(writes) == 2:
            raise OSError("模拟缓存更新失败")
        save(username, courses, now)

    monkeypatch.setattr(runner, "_save_class_cache", save_cache)
    with caplog.at_level(logging.WARNING):
        assert runner.run_class_signins_once()[0].ok
    assert "模拟缓存更新失败" in caplog.text
    assert runner.run_class_signins_once() == []
    client.sign_course.assert_called_once()


def test_one_failed_account_does_not_block_other_accounts(setup_runner, monkeypatch, caplog):
    store, client, create = setup_runner
    store.upsert_user(UserRecord(username="healthy-user"))
    store.upsert_user(UserRecord(username="disabled-user", enabled=False))
    store.save_json(SETTINGS_FILE, {"class_signin_enabled": True})
    runner = create()
    users = []

    def call(username, operation):
        users.append(username)
        if username == "audit-user":
            raise IclassApiError("会话暂不可用")
        return operation(Mock())

    monkeypatch.setattr(runner, "_with_reauth", call)
    with caplog.at_level(logging.WARNING):
        results = runner.run_class_signins_once()
    assert [result.username for result in results] == ["healthy-user"]
    assert "disabled-user" not in users
    assert "会话暂不可用" in caplog.text


def test_cli_toggle_preserves_other_settings(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.save_json(SETTINGS_FILE, {"auto_select_mode": "autonomous_sign_only"})
    monkeypatch.setenv("AUTOBOYA_HOME", str(tmp_path))
    runner = CliRunner()
    assert runner.invoke(cli.app, ["class-signin", "enable"]).exit_code == 0
    assert store.load_json(SETTINGS_FILE)["class_signin_enabled"] is True
    assert store.load_json(SETTINGS_FILE)["auto_select_mode"] == "autonomous_sign_only"
    assert runner.invoke(cli.app, ["class-signin", "disable"]).exit_code == 0
    assert store.load_json(SETTINGS_FILE)["class_signin_enabled"] is False


def test_account_removal_clears_class_cache(setup_runner):
    store, client, create = setup_runner
    create().query_class_signins("audit-user", Mock())
    assert store.remove_user("audit-user")
    assert store.load_json(ICLASS_CACHE_FILE) == {}

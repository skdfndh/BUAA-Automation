import json
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock

import pytest
from typer.testing import CliRunner

pytest.importorskip("auto_td")
from auto_td.storage import AppStorage
from auto_td.models import User, CheckResponse
from auto_td.constants import DEFAULT_WINDOWS
from autoboya.cli import app
from autoboya import td
from autoboya.models import UserRecord
from autoboya.storage import AutoBoyaStore
from autoboya.scheduler import AutomationRunner

NOW = datetime(2026, 10, 27, 15, 40, tzinfo=td.BEIJING_TZ)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(td, "parent_running", lambda store: True)
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="10000001"))
    store.save_json("settings.json", {"td_enabled": True, "class_signin_enabled": True, "auto_select_categories": ["美育"]})
    store.save_json("cache/iclass.json", {"10000001": {"updated_at": NOW.isoformat(), "courses": []}})
    storage = AppStorage(store.path("td"))
    storage.save_config({"server": {"ip": "127.0.0.1", "port": 1, "timeout": 2}, "type": "test", "schoolno": "test", "eventno": "test",
                         "machine": [{"id": 2, "machinesn": "IN"}, {"id": 6, "machinesn": "OUT"}]})
    storage.save_settings({"schedule": {"windows": DEFAULT_WINDOWS}, "integration": {
        "enabled": True, "term_id": "test-term", "target_count": 32, "start_date": "2026-10-27"}})
    user = User("10000001", "FAKECARD", 2, 6, "in.jpg", "out.jpg", wait_time_max=200)
    storage.save_users({user.student_id: user.to_dict()})
    storage.set_last_count(user.student_id, 6)
    storage.images_dir.mkdir()
    for name in ("in.jpg", "out.jpg"):
        (storage.images_dir / name).write_bytes(b"offline")
    client = Mock()
    client.check.return_value = CheckResponse(True, "打卡成功", 6)
    probe = MagicMock()
    return store, storage, user, client, probe


def test_eligible_tick_runs_one_action_only(setup):
    store, storage, _, client, probe = setup
    state, _ = td.td_tick(store, storage, NOW, client, probe)
    assert state == "waiting"
    client.check.assert_called_once()
    client.upload_photo.assert_called_once()
    td.td_tick(store, storage, NOW + timedelta(seconds=15), client, probe)
    assert client.check.call_count == 1


def test_network_probe_failure_does_not_submit_or_consume_progress(setup):
    store, storage, _, client, probe = setup
    probe.side_effect = TimeoutError("离线")
    before = storage.state_path.read_bytes()
    state, message = td.td_tick(store, storage, NOW, client, probe)
    assert state == "blocked" and "尚未提交" in message
    assert storage.state_path.read_bytes() == before
    client.check.assert_not_called()


def test_outside_window_never_probes_or_submits(setup):
    store, storage, _, client, probe = setup
    td.td_tick(store, storage, NOW.replace(hour=21), client, probe)
    probe.assert_not_called()
    client.check.assert_not_called()


def test_account_mismatch_blocks(setup):
    store, storage, _, client, probe = setup
    store.save_users([])
    assert td.td_tick(store, storage, NOW, client, probe)[0] == "blocked"
    client.check.assert_not_called()
    probe.assert_not_called()


def test_boya_conflict_defers_new_entrance(setup):
    store, storage, _, client, probe = setup
    store.save_json("cache/selected.json", {"10000001": [{"courseStartDate": "2026-10-27 15:30:00", "courseEndDate": "2026-10-27 16:30:00"}]})
    state, message = td.td_tick(store, storage, NOW, client, probe)
    assert state == "waiting" and "重叠" in message
    probe.assert_not_called()
    client.check.assert_not_called()


def test_stale_class_cache_defers_new_entrance(setup):
    store, storage, _, client, probe = setup
    store.save_json("cache/iclass.json", {})
    assert "刷新" in td.td_tick(store, storage, NOW, client, probe)[1]
    client.check.assert_not_called()


def test_pending_exit_is_not_delayed_by_class_conflict(setup):
    store, storage, user, client, probe = setup
    store.save_json("cache/iclass.json", {})
    state = storage.load_state()
    state["daily_runs"] = {"2026-10-27": {"users": {user.student_id: {"status": "waiting", "phase": "idle", "next_action": "exit",
                         "completed_rounds": 0, "due_at": (NOW - timedelta(seconds=1)).isoformat()}}}}
    storage.save_state(state)
    td.td_tick(store, storage, NOW, client, probe)
    assert client.check.call_args.args[1] == 6


def test_unknown_current_term_count_prevents_submission(setup):
    store, storage, _, client, probe = setup
    storage.save_state({"counts": {}})
    assert "可信次数" in td.td_tick(store, storage, NOW, client, probe)[1]
    client.check.assert_not_called()


def test_first_day_waits_until_confirmed_activation_date(setup):
    store, storage, _, client, probe = setup
    settings = storage.load_settings()
    settings["integration"]["start_date"] = "2026-10-28"
    storage.save_settings(settings)
    assert "2026-10-28" in td.td_tick(store, storage, NOW, client, probe)[1]
    client.check.assert_not_called()
    probe.assert_not_called()


def test_existing_error_is_not_cleared_or_retried(setup):
    store, storage, user, client, probe = setup
    state = storage.load_state()
    state["daily_runs"] = {"2026-10-27": {"users": {user.student_id: {"status": "error", "phase": "check_in_flight", "last_error": "结果未知"}}}}
    storage.save_state(state)
    assert td.td_tick(store, storage, NOW, client, probe) == ("error", "结果未知")
    client.check.assert_not_called()
    probe.assert_not_called()


def test_status_is_read_only_and_masks_account(setup):
    store, storage, user, _, _ = setup
    before = storage.state_path.read_bytes()
    result = td.collect_td_status(store)
    assert result["users"][0]["count"] == 6
    assert user.student_id not in json.dumps(result)
    assert storage.state_path.read_bytes() == before


def test_empty_status_does_not_create_directories(tmp_path):
    store = AutoBoyaStore(tmp_path / "missing")
    assert td.collect_td_status(store)["enabled"] is False
    assert not store.root.exists()


def test_worker_uses_correct_home_and_starts_as_separate_process(setup, monkeypatch):
    store, _, _, _, _ = setup
    spawn = Mock()
    monkeypatch.setattr(td.subprocess, "Popen", spawn)
    td.ensure_td_worker(store)
    assert spawn.call_args.args[0][-1] == "td-worker"
    assert spawn.call_args.kwargs["env"]["AUTOBOYA_HOME"] == str(store.root.resolve())


def test_running_worker_is_not_spawned_twice(setup, monkeypatch):
    store, _, _, _, _ = setup
    spawn = Mock()
    monkeypatch.setattr(td.subprocess, "Popen", spawn)
    with td.td_store(store).lock("run/autoboya.lock"):
        td.ensure_td_worker(store)
    spawn.assert_not_called()


def test_switches_preserve_boya_settings_and_errors(setup, monkeypatch):
    store, storage, _, _, _ = setup
    monkeypatch.setenv("AUTOBOYA_HOME", str(store.root))
    before = storage.state_path.read_bytes()
    runner = CliRunner()
    assert runner.invoke(app, ["td-disable"]).exit_code == 0
    assert store.load_json("settings.json")["td_enabled"] is False
    assert runner.invoke(app, ["td-enable"]).exit_code == 0
    assert store.load_json("settings.json")["auto_select_categories"] == ["美育"]
    assert store.load_json("settings.json")["class_signin_enabled"] is True
    assert storage.state_path.read_bytes() == before


def test_boya_loop_starts_td_without_executing_td_inline(setup, monkeypatch):
    store, _, _, _, _ = setup
    runner = AutomationRunner(store)
    start = Mock()
    tick = Mock(side_effect=AssertionError("不能在博雅循环里执行 TD 请求"))
    monkeypatch.setattr(td, "ensure_td_worker", start)
    monkeypatch.setattr(td, "td_tick", tick)
    monkeypatch.setattr(runner, "scan_once", lambda: [])
    monkeypatch.setattr(runner, "refresh_once", lambda **kwargs: None)
    monkeypatch.setattr(runner, "run_class_signins_once", lambda: [])
    monkeypatch.setattr("autoboya.scheduler.time.sleep", lambda seconds: runner.request_stop())
    runner.run_forever()
    start.assert_called_once_with(store)
    tick.assert_not_called()


def test_alerts_deduplicate_and_allow_same_issue_after_recovery(setup, monkeypatch):
    store, _, _, _, _ = setup
    report = {"enabled": True, "running": True, "heartbeat": NOW.isoformat(), "state": "error", "message": "入口结果未知", "users": []}
    monkeypatch.setattr(td, "collect_td_status", lambda store: report)
    assert len(td.check_td_alerts(store, NOW)) == 1
    assert td.check_td_alerts(store, NOW + timedelta(seconds=10)) == []
    report.update(state="waiting", message="等待下一动作")
    assert td.check_td_alerts(store, NOW + timedelta(seconds=20)) == []
    report.update(state="error", message="入口结果未知")
    assert len(td.check_td_alerts(store, NOW + timedelta(seconds=30))) == 1


@pytest.mark.parametrize("message", ["尚未到启用日期（2026-10-28）", "与已选博雅课程或签到窗口重叠", "等待普通课程日程成功刷新后再启动 TD 入口"])
def test_normal_waiting_does_not_alert(setup, monkeypatch, message):
    store, _, _, _, _ = setup
    monkeypatch.setattr(td, "collect_td_status", lambda store: {"enabled": True, "running": True, "heartbeat": NOW.isoformat(),
                       "state": "waiting", "message": message, "users": []})
    assert td.check_td_alerts(store, NOW) == []


def test_stale_heartbeat_alert_is_stable_and_does_not_spam(setup, monkeypatch):
    store, _, _, _, _ = setup
    monkeypatch.setattr(td, "collect_td_status", lambda store: {"enabled": True, "running": True, "heartbeat": NOW.isoformat(),
                       "state": "waiting", "message": "等待", "users": []})
    assert td.check_td_alerts(store, NOW + timedelta(minutes=6))[0]["kind"] == "heartbeat"
    assert td.check_td_alerts(store, NOW + timedelta(minutes=8)) == []


def test_disabled_td_does_not_alert(setup, monkeypatch):
    store, _, _, _, _ = setup
    monkeypatch.setattr(td, "collect_td_status", lambda store: {"enabled": False})
    assert td.check_td_alerts(store, NOW) == []


def test_corrupt_global_settings_cannot_look_like_disabled_healthy_td(setup):
    store, _, _, _, _ = setup
    store.path("settings.json").write_text("{broken", encoding="utf-8")
    alerts = td.check_td_alerts(store, NOW)
    assert alerts and alerts[0]["kind"] == "settings"


def test_dead_parent_is_reported_even_when_td_is_alive(setup, monkeypatch):
    store, _, _, _, _ = setup
    monkeypatch.setattr(td, "parent_running", lambda store: False)
    monkeypatch.setattr(td, "collect_td_status", lambda store: {"enabled": True, "running": True, "heartbeat": NOW.isoformat(),
                       "state": "waiting", "message": "等待", "users": []})
    assert any(issue["kind"] == "parent" for issue in td.check_td_alerts(store, NOW + timedelta(days=1)))


def test_naive_heartbeat_is_interpreted_as_beijing_time(setup, monkeypatch):
    store, _, _, _, _ = setup
    monkeypatch.setattr(td, "collect_td_status", lambda store: {"enabled": True, "running": True, "heartbeat": NOW.replace(tzinfo=None).isoformat(),
                       "state": "waiting", "message": "等待", "users": []})
    assert td.check_td_alerts(store, NOW) == []


def test_td_health_message_does_not_expose_card_id(setup):
    store, _, user, _, _ = setup
    store.save_json("td/health.json", {"state": "error", "message": f"失败：{user.card_id}"})
    assert user.card_id not in json.dumps(td.collect_td_status(store))

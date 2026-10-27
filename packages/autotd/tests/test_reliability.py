import json
import logging
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock

import pytest

from auto_td.client import TDClient, is_success_message
from auto_td.constants import BEIJING_TZ, DEFAULT_WINDOWS
from auto_td.models import User, CheckResponse
from auto_td.scheduler import run_scheduler_once, clear_today_errors, in_any_window
from auto_td.storage import AppStorage
from auto_td import telemetry
from auto_td.logging_utils import setup_logging

NOW = datetime(2026, 10, 27, 15, 40, tzinfo=BEIJING_TZ)


@pytest.fixture
def setup(tmp_path):
    storage = AppStorage(tmp_path)
    storage.save_config({"type": "test", "schoolno": "test", "eventno": "test", "server": {"ip": "127.0.0.1", "port": 1, "timeout": 2},
                         "machine": [{"id": 2, "machinesn": "IN"}, {"id": 6, "machinesn": "OUT"}]})
    storage.save_settings({"schedule": {"windows": DEFAULT_WINDOWS}, "integration": {
        "enabled": True, "term_id": "2026-test", "start_date": "2026-10-27", "target_count": 32}})
    user = User("10000001", "FAKECARD", 2, 6, "in.jpg", "out.jpg", wait_time_max=200)
    storage.save_users({user.student_id: user.to_dict()})
    storage.images_dir.mkdir()
    for name in ("in.jpg", "out.jpg"):
        (storage.images_dir / name).write_bytes(b"offline-test")
    storage.set_last_count(user.student_id, 6)
    client = Mock()
    client.check.return_value = CheckResponse(True, "打卡成功", 6)
    client.upload_photo.return_value = {"status": "success"}
    return storage, user, client


@pytest.mark.parametrize("message", ["打卡成功", "刷卡成功，本学期锻炼次数：6", "入口成功", "出口成功", "成功", "成功！"])
def test_explicit_success(message):
    assert is_success_message(message)


@pytest.mark.parametrize("message", ["打卡未成功", "不成功", "失败后成功", "本学期锻炼次数：6", "成功次数：0", "打卡成功次数：0", "刷卡成功率：0", "", None, "拒绝", "无效", "操作异常"])
def test_negative_or_unclear_result_is_not_success(message):
    assert not is_success_message(message)


def test_refresh_count_never_submits(setup):
    storage, user, _ = setup
    client = TDClient(storage.load_config())
    client.request = Mock()
    with pytest.raises(ValueError, match="已禁用"):
        client.query_count(user)
    client.request.assert_not_called()


def test_telemetry_cannot_collect_or_send_even_if_old_state_enabled(setup):
    storage, _, _ = setup
    state = storage.load_state()
    state["telemetry"] = {"enabled": True, "endpoint": "https://invalid.example"}
    storage.save_state(state)
    transport = Mock()
    with patch("urllib.request.urlopen", side_effect=AssertionError("禁止网络")):
        assert telemetry.ensure_telemetry_state(storage)["enabled"] is False
        assert telemetry.enqueue_event(storage, "test", {"student_id": "fake"}) is None
        assert telemetry.enqueue_install_initialized(storage) is None
        assert telemetry.flush_telemetry_queue(storage, transport) == 0
        with pytest.raises(ValueError):
            telemetry.enable_telemetry(storage)
        with pytest.raises(RuntimeError):
            telemetry.UrlJsonTransport().post_json("https://invalid.example", {})
    transport.post_json.assert_not_called()
    assert not storage.telemetry_queue_path.exists()


def test_atomic_state_write_preserves_original_on_failure(setup):
    storage, _, _ = setup
    before = storage.state_path.read_bytes()
    with patch("auto_td.storage.json.dump", side_effect=OSError("模拟写盘失败")):
        with pytest.raises(OSError):
            storage.save_state({"new": True})
    assert storage.state_path.read_bytes() == before
    assert not list(storage.home.glob("*.tmp"))


def test_actions_are_locked(setup):
    storage, _, client = setup
    with storage.action_lock():
        with pytest.raises(RuntimeError, match="另一个"):
            run_scheduler_once(AppStorage(storage.home), now=NOW, client=client)
    client.check.assert_not_called()


def test_old_term_count_does_not_disable_or_enable_current_term(setup):
    storage, user, client = setup
    state = storage.load_state()
    state["counts"][user.student_id].update(count=32, term_id="old-term")
    storage.save_state(state)
    assert storage.get_last_count(user.student_id) is None
    result = run_scheduler_once(storage, now=NOW, client=client)
    assert "可信次数" in result.results[0].message
    client.check.assert_not_called()


def test_count_regression_requires_manual_confirmation(setup):
    storage, user, _ = setup
    with pytest.raises(ValueError, match="回退"):
        storage.set_last_count(user.student_id, 0)
    assert storage.get_last_count(user.student_id) == 6


def test_confirmed_entrance_is_not_repeated_after_photo_error_and_restart(setup):
    storage, user, client = setup
    client.upload_photo.side_effect = TimeoutError("模拟上传响应丢失")
    run_scheduler_once(storage, now=NOW, client=client)
    state = storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]
    assert state["status"] == "error"
    assert state["phase"] == "photo_in_flight"
    clear_today_errors(AppStorage(storage.home), NOW)
    run_scheduler_once(AppStorage(storage.home), now=NOW + timedelta(minutes=5), client=client)
    assert client.check.call_count == 1
    assert client.upload_photo.call_count == 1


@pytest.mark.parametrize("phase", ["check_in_flight", "check_confirmed", "photo_in_flight", "photo_confirmed"])
def test_crash_checkpoint_never_resubmits_uncertain_action(setup, phase):
    storage, user, client = setup
    state = storage.load_state()
    state["daily_runs"] = {"2026-10-27": {"users": {user.student_id: {"status": "pending", "phase": phase}}}}
    storage.save_state(state)
    run_scheduler_once(storage, now=NOW, client=client)
    assert storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]["status"] == "error"
    client.check.assert_not_called()
    client.upload_photo.assert_not_called()


def test_intent_is_saved_before_check_and_upload(setup):
    storage, user, client = setup
    def current_phase():
        return storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]["phase"]
    def check(*args, **kwargs):
        assert current_phase() == "check_in_flight"
        return CheckResponse(True, "打卡成功", 6)
    def upload(*args, **kwargs):
        assert current_phase() == "photo_in_flight"
    client.check.side_effect = check
    client.upload_photo.side_effect = upload
    run_scheduler_once(storage, now=NOW, client=client)
    assert current_phase() == "idle"


def test_missing_photo_prevents_check_submission(setup):
    storage, _, client = setup
    (storage.images_dir / "in.jpg").unlink()
    run_scheduler_once(storage, now=NOW, client=client)
    client.check.assert_not_called()


def test_three_rounds_stop_for_today_and_survive_restart(setup):
    storage, user, client = setup
    for step in range(8):
        run_scheduler_once(AppStorage(storage.home), now=NOW + timedelta(minutes=4 * step), client=client,
                           wait_seconds=lambda user: 180)
    assert client.check.call_count == 6
    assert [call.args[1] for call in client.check.call_args_list] == [2, 6, 2, 6, 2, 6]
    assert client.upload_photo.call_count == 6
    state = storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]
    assert state["status"] == "completed" and state["completed_rounds"] == 3


def test_reaching_quota_at_entrance_still_finishes_paired_exit(setup):
    storage, user, client = setup
    storage.set_last_count(user.student_id, 31)
    client.check.return_value = CheckResponse(True, "打卡成功", 32)
    run_scheduler_once(storage, now=NOW, client=client, wait_seconds=lambda user: 180)
    run_scheduler_once(storage, now=NOW + timedelta(minutes=4), client=client)
    run_scheduler_once(storage, now=NOW + timedelta(minutes=8), client=client)
    assert [call.args[1] for call in client.check.call_args_list] == [2, 6]
    state = storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]
    assert state["status"] == "completed" and state["completed_rounds"] == 1


def test_quota_already_reached_does_not_fake_completed_rounds(setup):
    storage, user, client = setup
    storage.set_last_count(user.student_id, 32)
    run_scheduler_once(storage, now=NOW, client=client)
    assert storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]["completed_rounds"] == 0
    client.check.assert_not_called()


def test_too_late_to_finish_pair_does_not_start(setup):
    storage, _, client = setup
    run_scheduler_once(storage, now=NOW.replace(hour=19, minute=58), client=client)
    client.check.assert_not_called()


def test_window_end_is_exclusive():
    assert in_any_window(NOW.replace(hour=9, minute=59), DEFAULT_WINDOWS)
    assert not in_any_window(NOW.replace(hour=10, minute=0), DEFAULT_WINDOWS)


def test_expired_exit_blocks_instead_of_starting_a_new_entrance(setup):
    storage, user, client = setup
    state = storage.load_state()
    state["daily_runs"] = {"2026-10-27": {"users": {user.student_id: {
        "status": "waiting", "phase": "idle", "next_action": "exit", "due_at": "2026-10-27T20:01:00+08:00"}}}}
    storage.save_state(state)
    run_scheduler_once(storage, now=NOW.replace(hour=20, minute=2), client=client)
    run_scheduler_once(storage, now=NOW + timedelta(days=1), client=client)
    assert storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]["status"] == "error"
    client.check.assert_not_called()


def test_logs_hide_account_and_card_even_in_exceptions(setup):
    storage, user, _ = setup
    logger = setup_logging(storage, now=NOW, stream=False)
    logger.error("测试账号 %s 卡号 %s", user.student_id, user.card_id)
    text = next(storage.logs_dir.glob("*.txt")).read_text(encoding="utf-8")
    assert user.student_id not in text and user.card_id not in text


def test_response_length_is_bounded(setup):
    import struct
    storage, _, _ = setup
    sock = MagicMock()
    sock.recv.return_value = struct.pack(">lB", 10_000_000, 80)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = sock
    with pytest.raises(ValueError, match="长度"):
        TDClient(storage.load_config(), socket_factory=factory).request(b"test", 80)
    assert sock.recv.call_count == 1


def test_exit_without_count_pauses_following_rounds(setup):
    storage, user, client = setup
    client.check.side_effect = [CheckResponse(True, "入口成功", 6), CheckResponse(True, "出口成功", None)]
    run_scheduler_once(storage, now=NOW, client=client, wait_seconds=lambda user: 180)
    run_scheduler_once(storage, now=NOW + timedelta(minutes=4), client=client)
    run_scheduler_once(storage, now=NOW + timedelta(minutes=8), client=client)
    state = storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]
    assert state["status"] == "error" and "未返回累计次数" in state["last_error"]
    assert client.check.call_count == 2


@pytest.mark.parametrize("entrance,exit", [(6, 2), (2, 11), (99, 6)])
def test_invalid_machine_pairs_are_rejected_before_submission(setup, entrance, exit):
    from dataclasses import replace
    storage, user, client = setup
    changed = replace(user, entrance_machine_id=entrance, exit_machine_id=exit)
    storage.save_users({changed.student_id: changed.to_dict()})
    run_scheduler_once(storage, now=NOW, client=client)
    client.check.assert_not_called()


def test_missing_exit_photo_also_prevents_new_entrance(setup):
    storage, _, client = setup
    (storage.images_dir / "out.jpg").unlink()
    run_scheduler_once(storage, now=NOW, client=client)
    client.check.assert_not_called()


@pytest.mark.parametrize("expected,now", [("2026-10-27T13:30:00+08:00", NOW),
    ("2026-10-27T15:32:00+08:00", NOW), (None, NOW)])
def test_sleep_or_missing_timing_cannot_submit_an_expired_exit(setup, expected, now):
    storage, user, client = setup
    state = storage.load_state()
    state["daily_runs"] = {"2026-10-27": {"users": {user.student_id: {"status": "waiting", "phase": "idle", "next_action": "exit",
                         "completed_rounds": 0, "due_at": expected}}}}
    storage.save_state(state)
    run_scheduler_once(storage, now=now, client=client)
    client.check.assert_not_called()
    assert storage.load_state()["daily_runs"]["2026-10-27"]["users"][user.student_id]["status"] == "error"

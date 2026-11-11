from unittest.mock import Mock

import pytest

from autoboya import login_reminder, scheduler
from autoboya.exceptions import InteractiveLoginRequired, SessionExpired
from autoboya.storage import AutoBoyaStore


def test_one_popup_for_both_services_then_rearm_after_login(monkeypatch, tmp_path):
    store = AutoBoyaStore(tmp_path)
    store.init()
    runner = scheduler.AutomationRunner(store)
    runner._reminder = login_reminder.LoginReminder(store)
    child = Mock()
    child.poll.return_value = None
    launch = Mock(return_value=child)
    monkeypatch.setattr(login_reminder.subprocess, "Popen", launch)
    clock = Mock(return_value=100)
    monkeypatch.setattr(login_reminder.time, "monotonic", clock)
    client = Mock()
    ensure = Mock(side_effect=[InteractiveLoginRequired(), client, InteractiveLoginRequired()])
    monkeypatch.setattr(scheduler, "ensure_bykc_client", ensure)
    for action in ("query_today", "query_statistics"):
        with pytest.raises(InteractiveLoginRequired):
            runner._with_reauth("demo", lambda c: getattr(c, action)())
    assert launch.call_count == ensure.call_count == 1
    clock.return_value += 700
    runner._reminder.notify("demo", None)
    assert launch.call_count == 1
    child.poll.return_value = child.returncode = 0
    runner._reminder.notify("demo", None)
    clock.return_value += 599
    runner._reminder.notify("demo", None)
    assert launch.call_count == 1
    clock.return_value += 1
    runner._reminder.notify("demo", None)
    assert launch.call_count == 2
    store.save_json("sessions/demo.json", {})
    assert runner._with_reauth("demo", lambda c: c) is client
    with pytest.raises(InteractiveLoginRequired):
        runner._with_reauth("demo", lambda c: c)
    assert launch.call_count == 3


def test_only_prompt_if_silent_renewal_cannot_restore_session(monkeypatch, tmp_path):
    runner = scheduler.AutomationRunner(AutoBoyaStore(tmp_path))
    runner._reminder = Mock()
    monkeypatch.setattr(scheduler, "ensure_bykc_client", Mock())
    monkeypatch.setattr(scheduler, "force_login_bykc_client", Mock())
    assert runner._with_reauth("demo", Mock(side_effect=[SessionExpired(), "ok"])) == "ok"
    with pytest.raises(TimeoutError):
        runner._with_reauth("demo", Mock(side_effect=TimeoutError()))
    runner._reminder.notify.assert_not_called()
    with pytest.raises(SessionExpired):
        runner._with_reauth("demo", Mock(side_effect=SessionExpired()))
    runner._reminder.notify.assert_called_once_with("demo", None)

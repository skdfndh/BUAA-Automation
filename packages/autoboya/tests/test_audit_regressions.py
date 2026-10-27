from unittest.mock import Mock

import pytest

from autoboya import session, scheduler
from autoboya.exceptions import SessionExpired
from autoboya.storage import AutoBoyaStore


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_request_clients_are_closed_on_success_and_failure(tmp_path, monkeypatch, background, failure):
    store = AutoBoyaStore(tmp_path)
    client = Mock()
    monkeypatch.setattr(session if not background else scheduler, "ensure_bykc_client", lambda *args, **kwargs: client)
    def operation(value):
        assert value is client
        if failure:
            raise RuntimeError("模拟请求失败")
        return "ok"
    call = (lambda: scheduler.AutomationRunner(store)._with_reauth("audit", operation)) if background else (
        lambda: session.call_with_reauth(store, "audit", operation))
    if failure:
        with pytest.raises(RuntimeError):
            call()
    else:
        assert call() == "ok"
    client.close.assert_called_once()


@pytest.mark.parametrize("background", [False, True])
def test_expired_client_is_closed_before_refresh_and_new_one_also_closes(tmp_path, monkeypatch, background):
    store = AutoBoyaStore(tmp_path)
    old, fresh = Mock(), Mock()
    module = scheduler if background else session
    monkeypatch.setattr(module, "ensure_bykc_client", lambda *args, **kwargs: old)
    def refresh(*args, **kwargs):
        old.close.assert_called_once()
        return fresh
    monkeypatch.setattr(module, "force_login_bykc_client", refresh)
    def operation(client):
        if client is old:
            raise SessionExpired("会话过期")
        return "ok"
    result = scheduler.AutomationRunner(store)._with_reauth("audit", operation) if background else session.call_with_reauth(store, "audit", operation)
    assert result == "ok"
    fresh.close.assert_called_once()


def test_cleanup_failure_cannot_change_confirmed_success(tmp_path, monkeypatch):
    client = Mock()
    client.close.side_effect = RuntimeError("模拟清理失败")
    monkeypatch.setattr(session, "ensure_bykc_client", lambda *args, **kwargs: client)
    assert session.call_with_reauth(AutoBoyaStore(tmp_path), "audit", lambda value: "confirmed") == "confirmed"

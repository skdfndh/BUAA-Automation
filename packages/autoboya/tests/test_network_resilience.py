import json
from unittest.mock import Mock

import httpx
import pytest

from autoboya import bykc, scheduler
from autoboya.bykc import BykcClient
from autoboya.config import COURSE_CACHE_FILE, STOP_FILE
from autoboya.crypto import EncryptedRequest
from autoboya.exceptions import BoyaApiError
from autoboya.models import BoyaCourse, UserRecord
from autoboya.scheduler import AutomationRunner
from autoboya.storage import AutoBoyaStore


@pytest.fixture(autouse=True)
def mock_crypto_and_retry_wait(monkeypatch):
    keys = []

    class Crypto:
        def __init__(self):
            keys.append(len(keys))
            self.index = keys[-1]

        def encrypt_request(self, payload):
            return EncryptedRequest(json.dumps(payload).encode(), {"X-Test-Request": str(self.index)})

        def decrypt_response(self, body):
            return json.loads(body)

    monkeypatch.setattr(bykc, "BykcCrypto", Crypto)
    monkeypatch.setattr(bykc.time, "sleep", lambda seconds: None)
    return keys


@pytest.mark.parametrize("api", sorted(bykc.READ_APIS))
def test_transient_read_failure_is_retried_with_fresh_request(api, mock_crypto_and_retry_wait):
    calls = []

    def handler(request):
        calls.append(request.headers["X-Test-Request"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("模拟读取超时")
        return httpx.Response(200, json={"status": "0"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert BykcClient("mock", http_client=http, use_vpn=False).call(api)["status"] == "0"
    assert calls == ["0", "1"]


@pytest.mark.parametrize("api", ["choseCourse", "signCourseByUser", "delChosenCourse", "unknownApi"])
@pytest.mark.parametrize("failure", ["timeout", "gateway"])
def test_mutations_and_unknown_calls_are_not_blindly_retried(api, failure):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("模拟响应丢失")
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises((httpx.ReadTimeout, httpx.HTTPStatusError)):
            BykcClient("mock", http_client=http, use_vpn=False).call(api)
    assert len(calls) == 1


@pytest.mark.parametrize("status,expected_calls", [(502, 2), (503, 2), (504, 2), (400, 1), (403, 1)])
def test_read_retries_are_bounded_and_only_for_transient_gateway_errors(status, expected_calls):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(httpx.HTTPStatusError):
            BykcClient("mock", http_client=http, use_vpn=False).get_all_config()
    assert len(calls) == expected_calls


def test_small_pages_retry_only_failed_page_and_service_between_pages():
    pages = []
    serviced = []

    def handler(request):
        args = json.loads(request.content)
        assert args["pageSize"] == 20
        page = args["pageNumber"]
        pages.append(page)
        if page == 2 and pages.count(2) == 1:
            raise httpx.ReadTimeout("第二页暂时失败")
        start = (page - 1) * 20
        return httpx.Response(200, json={"status": "0", "data": {
            "content": [{"id": index} for index in range(start + 1, min(start + 20, 45) + 1)],
            "totalPages": 3, "totalElements": 45, "last": page == 3,
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        courses = BykcClient("mock", http_client=http, use_vpn=False).query_courses(after_page=lambda: serviced.append(True))
    assert len(courses) == 45
    assert pages == [1, 2, 2, 3]
    assert len(serviced) == 3


def test_incomplete_final_page_is_not_accepted():
    def handler(request):
        return httpx.Response(200, json={"status": "0", "data": {
            "content": [{"id": 1}], "last": True, "totalElements": 20,
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(BoyaApiError, match="不完整"):
            BykcClient("mock", http_client=http, use_vpn=False).query_courses()


def test_failed_refresh_preserves_previous_complete_cache(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="audit"))
    store.save_json(COURSE_CACHE_FILE, [{"id": 999, "courseName": "之前的完整缓存"}])

    def handler(request):
        page = json.loads(request.content)["pageNumber"]
        if page == 2:
            raise httpx.ReadTimeout("第二页超时")
        return httpx.Response(200, json={"status": "0", "data": {
            "content": [{"id": 1}], "totalPages": 2, "totalElements": 2,
        }})

    runner = AutomationRunner(store)
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = BykcClient("mock", http_client=http, use_vpn=False)
        monkeypatch.setattr(runner, "_with_reauth", lambda username, operation: operation(client))
        monkeypatch.setattr(runner, "_refresh_user_cache", lambda username: None)
        runner.refresh_once()
    assert store.load_json(COURSE_CACHE_FILE) == [{"id": 999, "courseName": "之前的完整缓存"}]


def test_sign_scans_between_pages_are_throttled_and_never_select_courses(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.upsert_user(UserRecord(username="audit"))
    runner = AutomationRunner(store)
    runner.cache.save_selected("audit", [BoyaCourse(id=1, sign_config={
        "signPointList": [{"lat": 40, "lng": 116}],
        "signStartDate": "2000-01-01 00:00:00", "signEndDate": "2100-01-01 00:00:00",
    })])
    executed = Mock()
    monkeypatch.setattr(runner, "execute_decisions", executed)
    monkeypatch.setattr(scheduler.time, "monotonic", lambda: 100)
    runner._service_due_signins()
    runner._service_due_signins()
    executed.assert_called_once()
    assert [decision.action for decision in executed.call_args.args[0]] == ["sign"]


def test_stop_request_cancels_remaining_pages(tmp_path):
    store = AutoBoyaStore(tmp_path)
    store.init()
    store.path(STOP_FILE).write_text("stop")
    runner = AutomationRunner(store)
    with pytest.raises(InterruptedError):
        runner._service_due_signins()


def test_pagination_uses_bounded_timeout_without_changing_mutation_timeout():
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        return httpx.Response(200, json={"status": "0", "data": {"content": [], "last": True}})

    with httpx.Client(transport=httpx.MockTransport(handler), timeout=25) as http:
        client = BykcClient("mock", http_client=http, use_vpn=False)
        client.query_courses()
        client.select_course(42)
    assert timeouts[0]["read"] == 15
    assert timeouts[0]["connect"] == 5
    assert timeouts[1]["read"] == 25


def test_empty_page_before_advertised_last_page_is_rejected():
    def handler(request):
        return httpx.Response(200, json={"status": "0", "data": {"content": [], "totalPages": 3, "last": False}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(BoyaApiError, match="提前结束"):
            BykcClient("mock", http_client=http, use_vpn=False).query_courses()


def test_stop_during_refresh_prevents_followup_actions(tmp_path, monkeypatch):
    store = AutoBoyaStore(tmp_path)
    store.init()
    runner = AutomationRunner(store)
    scan = Mock(return_value=[])
    execute = Mock()
    monkeypatch.setattr(runner, "scan_once", scan)
    monkeypatch.setattr(runner, "execute_decisions", execute)
    monkeypatch.setattr(runner, "refresh_once", lambda **kwargs: runner.request_stop())
    runner.run_forever()
    scan.assert_called_once()
    execute.assert_called_once()

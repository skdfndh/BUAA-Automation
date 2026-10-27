from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from autoboya.iclass import IclassApiError, IclassClient, IclassCourse, class_signin_candidate, extract_login_name
from autoboya.logging import redact
from autoboya.webvpn import from_webvpn_url, to_webvpn_url


@pytest.mark.parametrize("value,expected", [("abc+def/ghi=", "abc+def/ghi="), ("abc%2Bdef%2Fghi%3D", "abc+def/ghi=")])
def test_login_name_preserves_base64(value, expected):
    assert extract_login_name(f"https://iclass.buaa.edu.cn:8346/?loginname={value}#/MyCenter") == expected


@pytest.mark.parametrize("url", [
    "https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter#/MyCenter",
    "https://iclass.buaa.edu.cn:8347/app/course//get_stu_course_sched.action?id=abc%2Bdef",
    "http://iclass.buaa.edu.cn:8081/eschool/app/course/stu_scan_sign.action",
])
def test_vpn_roundtrip_preserves_port_path_query_and_fragment(url):
    assert from_webvpn_url(to_webvpn_url(url)) == url


@pytest.mark.parametrize("use_vpn", [True, False])
def test_iclass_login_query_sign_protocol_and_session_reuse(use_vpn):
    seen = []
    app_logins = []

    def handler(request):
        raw = from_webvpn_url(str(request.url))
        url = urlparse(raw)
        seen.append((request.method, raw))
        assert request.url.host == ("d.buaa.edu.cn" if use_vpn else "iclass.buaa.edu.cn")
        if url.port == 8346:
            return httpx.Response(302, headers={"location": "/?loginName=abc+def%2Fghi%3D&type=jumpMyCenter#/MyCenter"})
        if url.path.endswith("/user/login.action"):
            app_logins.append(request.url.params["phone"])
            assert request.url.params["password"] == ""
            return httpx.Response(200, json={"STATUS": 0, "result": {"id": "user-1", "sessionId": "session-1"}})
        if url.path.endswith("get_stu_course_sched.action"):
            assert request.headers["sessionId"] == "session-1"
            assert request.url.params["dateStr"] == "20261027"
            return httpx.Response(200, json={"STATUS": "0", "result": [{
                "id": "class-1", "courseName": "测试课程", "classBeginTime": "08:00", "classEndTime": "09:40", "signStatus": "0",
            }]})
        if url.path.endswith("get_timestamp.action"):
            assert url.port == (8347 if use_vpn else 8081)
            return httpx.Response(200, json={"timestamp": "1793000000"})
        assert url.path == ("/app/course/stu_scan_sign.action" if use_vpn else "/eschool/app/course/stu_scan_sign.action")
        assert url.port == (8347 if use_vpn else 8081)
        assert request.method == "POST"
        assert request.url.params["courseSchedId"] == "class-1"
        assert request.url.params["timestamp"] == "1793000000"
        assert parse_qs(request.content.decode()) == {"id": ["user-1"]}
        assert request.headers["sessionId"] == "session-1"
        return httpx.Response(200, json={"STATUS": 0, "ERRMSG": "签到成功", "result": {"stuSignStatus": "1"}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = IclassClient(http_client=http, use_vpn=use_vpn)
        assert client.query_today(datetime(2026, 10, 27))[0].id == "class-1"
        assert client.query_today(datetime(2026, 10, 27))[0].sign_status == 0
        assert client.sign_course("class-1") == "签到成功"
    assert app_logins == ["abc+def/ghi="]
    assert sum(method == "POST" for method, _ in seen) == 1


def test_sso_redirect_keeps_raw_host_and_nondefault_port():
    seen = []

    def handler(request):
        raw = from_webvpn_url(str(request.url))
        seen.append(raw)
        if len(seen) == 1:
            location = to_webvpn_url("https://sso.buaa.edu.cn/login?service=iclass")
        elif len(seen) == 2:
            location = "https://iclass.buaa.edu.cn:8346/cas-login?ticket=mock-ticket"
        elif len(seen) == 3:
            location = "/?type=jumpMyCenter#/MyCenter"
        else:
            location = to_webvpn_url("https://iclass.buaa.edu.cn:8346/?loginName=mock-name&type=jumpMyCenter").replace("https://d.buaa.edu.cn", "")
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert IclassClient(http_client=http).resolve_login_name() == "mock-name"
    assert seen[3] == "https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter#/MyCenter"


def test_sso_redirect_limit_and_untrusted_host_are_blocked():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "/loop"}))) as http:
        with pytest.raises(IclassApiError, match="超过 8 次"):
            IclassClient(http_client=http).resolve_login_name()
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://example.test/?loginName=mock-name"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(IclassApiError, match="非学校地址"):
            IclassClient(http_client=http).resolve_login_name()
    assert len(calls) == 1


@pytest.mark.parametrize("payload", [{}, {"STATUS": False, "result": []}, {"STATUS": "0"}, {"STATUS": "0", "result": {}}, []])
def test_invalid_query_response_is_not_reported_as_no_classes(payload):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as http:
        client = IclassClient(http_client=http)
        client.user_id, client.session_id = "user-1", "session-1"
        with pytest.raises(IclassApiError):
            client.query_today()


@pytest.mark.parametrize("status", [None, False, "unknown", 2])
def test_unknown_sign_status_never_defaults_to_unsigned(status):
    payload = {"STATUS": "0", "result": [{"id": "class-1", "signStatus": status}]}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as http:
        client = IclassClient(http_client=http)
        client.user_id, client.session_id = "user-1", "session-1"
        assert client.query_today()[0].sign_status is None


@pytest.mark.parametrize("timestamp", [None, "", False, "invalid"])
def test_missing_server_time_does_not_submit_sign(timestamp):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"timestamp": timestamp})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = IclassClient(http_client=http)
        client.user_id, client.session_id = "user-1", "session-1"
        with pytest.raises(IclassApiError, match="服务器时间"):
            client.sign_course("class-1")
    assert calls == ["GET"]


@pytest.mark.parametrize("payload", [{"STATUS": "0", "result": {"stuSignStatus": "0"}},
                                          {"STATUS": "0", "result": {"stuSignStatus": True}},
                                          {"result": {"stuSignStatus": "1"}}])
def test_sign_requires_both_success_status_and_signed_result(payload):
    def handler(request):
        return httpx.Response(200, json={"timestamp": "1793000000"} if request.method == "GET" else payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = IclassClient(http_client=http)
        client.user_id, client.session_id = "user-1", "session-1"
        with pytest.raises(IclassApiError, match="未确认签到成功"):
            client.sign_course("class-1")


def test_sign_timeout_is_not_blindly_retried():
    posts = []

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"timestamp": "1793000000"})
        posts.append(request)
        raise httpx.ReadTimeout("mock timeout")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = IclassClient(http_client=http)
        client.user_id, client.session_id = "user-1", "session-1"
        with pytest.raises(httpx.ReadTimeout):
            client.sign_course("class-1")
    assert len(posts) == 1


def test_expired_app_session_relogs_once():
    queries = []

    def handler(request):
        path = from_webvpn_url(str(request.url))
        if "jumpMyCenter" in path:
            return httpx.Response(302, headers={"location": "/?loginName=mock-name"})
        if "login.action" in path:
            return httpx.Response(200, json={"STATUS": "0", "result": {"id": "user-1", "sessionId": "new-session"}})
        queries.append(request.headers["sessionId"])
        return httpx.Response(200, json={"STATUS": "1", "ERRMSG": "请重新登录"} if len(queries) == 1 else {"STATUS": "0", "result": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = IclassClient(http_client=http)
        client.user_id, client.session_id = "user-1", "old-session"
        assert client.query_today() == []
    assert queries == ["old-session", "new-session"]


@pytest.mark.parametrize("begin,end,sign_status,expected", [
    ("09:50", "11:25", 0, True), ("09:50:00", "11:25:00", 0, True),
    ("2026-10-27 09:50:00", "2026-10-27 11:25:00", 0, True),
    ("08:00", "09:40", 0, False), ("10:30", "12:00", 0, False),
    ("09:50", "11:25", 1, False), ("09:50", "11:25", None, False),
    ("", "", 0, False), ("2026-10-26 09:50:00", "2026-10-26 11:25:00", 0, False),
])
def test_candidate_requires_current_class_window_and_explicit_unsigned(begin, end, sign_status, expected):
    course = IclassCourse("class-1", "测试课程", begin, end, sign_status)
    assert class_signin_candidate(course, datetime(2026, 10, 27, 10)) is expected


def test_iclass_login_credentials_are_redacted():
    value = redact("https://example.test/login?phone=private-login-name&loginName=private-name sessionId=private-session")
    assert "private-" not in value


def test_last_allowed_sso_redirect_can_return_login_name():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/?loginName=mock-name" if len(calls) == 8 else "/loop"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert IclassClient(http_client=http).resolve_login_name() == "mock-name"
    assert len(calls) == 8


@pytest.mark.parametrize("course_id", ["", "class 1", None])
def test_invalid_course_id_never_makes_network_request(course_id):
    requests = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: requests.append(request))) as http:
        with pytest.raises(IclassApiError, match="排课 ID"):
            IclassClient(http_client=http).sign_course(course_id)
    assert requests == []


def test_failed_clock_status_prevents_submission_even_if_timestamp_is_present():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"STATUS": "1", "timestamp": "1793000000"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        client = IclassClient(http_client=http)
        client.user_id, client.session_id = "user-1", "session-1"
        with pytest.raises(IclassApiError, match="服务器时间失败"):
            client.sign_course("class-1")
    assert [request.method for request in requests] == ["GET"]

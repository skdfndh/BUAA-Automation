from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urljoin, urlparse

import httpx

from .bykc import looks_like_login_page, restore_cookies
from .exceptions import AutoBoyaError, SessionExpired
from .http import new_http_client
from .webvpn import WEBVPN_GATEWAY, from_webvpn_url, to_webvpn_url

# 登录与签到协议参考 UBAA，来源提交和 MIT 许可见 NOTICE.md。
MY_CENTER = "https://iclass.buaa.edu.cn:8346/?type=jumpMyCenter"
APP_BASE = "https://iclass.buaa.edu.cn:8347/app/"
AUTH_HOSTS = {"iclass.buaa.edu.cn", "sso.buaa.edu.cn", "uc.buaa.edu.cn", "d.buaa.edu.cn"}


class IclassApiError(AutoBoyaError):
    pass


@dataclass
class IclassCourse:
    id: str
    name: str
    begin: str
    end: str
    sign_status: int | None


def extract_login_name(url: str) -> str | None:
    # Base64 中的加号是令牌的一部分，不能使用会把加号转为空格的表单解码。
    for part in urlparse(url).query.split("&"):
        key, _, value = part.partition("=")
        if unquote(key).lower() == "loginname" and value:
            return unquote(value)
    return None


def class_signin_candidate(course: IclassCourse, now: datetime) -> bool:
    if type(course.sign_status) is not int or course.sign_status != 0:
        return False
    try:
        begin = datetime.fromisoformat(f"{now.date()}T{course.begin}" if len(course.begin) <= 8 else course.begin)
        end = datetime.fromisoformat(f"{now.date()}T{course.end}" if len(course.end) <= 8 else course.end)
        return begin.date() == now.date() and begin <= now <= end
    except (ValueError, TypeError):
        return False


class IclassClient:
    def __init__(self, cookies: list[dict] | None = None, http_client: httpx.Client | None = None, use_vpn: bool = True):
        self.client = http_client or new_http_client()
        self.use_vpn = use_vpn
        self.user_id: str | None = None
        self.session_id: str | None = None
        if cookies:
            restore_cookies(self.client, cookies)

    def close(self) -> None:
        self.client.close()

    def upstream(self, url: str) -> str:
        return to_webvpn_url(url) if self.use_vpn else url

    def resolve_login_name(self) -> str:
        current = MY_CENTER
        for _ in range(8):
            if urlparse(current).hostname not in AUTH_HOSTS:
                raise IclassApiError("iClass 登录跳转到了非学校地址，已停止")
            name = extract_login_name(current)
            if name:
                return name
            response = self.client.get(self.upstream(current), follow_redirects=False)
            if response.status_code == 401 or looks_like_login_page(response.text):
                raise SessionExpired("iClass 需要重新登录学校统一认证")
            location = response.headers.get("location")
            if not (300 <= response.status_code < 400 and location):
                raise IclassApiError("iClass 登录跳转未返回 loginName")
            if location.startswith(("/https/", "/https-", "/http/", "/http-")):
                location = urljoin(WEBVPN_GATEWAY, location)
            current = urljoin(from_webvpn_url(str(response.url)), from_webvpn_url(location))
            if urlparse(current).hostname not in AUTH_HOSTS:
                raise IclassApiError("iClass 登录跳转到了非学校地址，已停止")
            name = extract_login_name(current)
            if name:
                return name
        raise IclassApiError("iClass 登录跳转超过 8 次，已停止")

    def _json(self, method: str, url: str, **kwargs) -> dict:
        response = self.client.request(method, self.upstream(url), follow_redirects=False, **kwargs)
        if response.status_code == 401 or 300 <= response.status_code < 400 or looks_like_login_page(response.text):
            raise SessionExpired("iClass 会话已失效")
        if response.status_code != 200:
            raise IclassApiError(f"iClass 接口返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise IclassApiError("iClass 接口未返回有效 JSON") from exc
        if not isinstance(payload, dict):
            raise IclassApiError("iClass 响应格式不正确")
        message = str(payload.get("ERRMSG") or "")
        if any(word in message for word in ("未登录", "重新登录", "会话失效", "登录失效", "登录超时")):
            raise SessionExpired("iClass 会话已失效")
        return payload

    @staticmethod
    def _success(payload: dict) -> bool:
        value = payload.get("STATUS")
        return type(value) in (str, int) and str(value) in {"0", "200", "success"}

    def login(self) -> None:
        name = self.resolve_login_name()
        payload = self._json("GET", APP_BASE + "user/login.action", params={
            "password": "", "phone": name, "userLevel": "1", "verificationType": "2", "verificationUrl": "",
        })
        result = payload.get("result")
        if (not self._success(payload) or not isinstance(result, dict)
                or type(result.get("id")) not in (str, int) or not str(result["id"]).strip()
                or type(result.get("sessionId")) not in (str, int) or not str(result["sessionId"]).strip()):
            raise IclassApiError("iClass app 登录失败，未获得有效会话")
        self.user_id, self.session_id = str(result["id"]), str(result["sessionId"])

    def _with_session(self, operation):
        if not self.session_id:
            self.login()
        try:
            return operation()
        except SessionExpired:
            self.user_id = self.session_id = None
            self.login()
            return operation()

    def query_today(self, now: datetime | None = None) -> list[IclassCourse]:
        now = now or datetime.now()

        def query():
            payload = self._json("GET", APP_BASE + "course/get_stu_course_sched.action",
                                 headers={"sessionId": self.session_id},
                                 params={"id": self.user_id, "dateStr": now.strftime("%Y%m%d")})
            if not self._success(payload) or not isinstance(payload.get("result"), list):
                raise IclassApiError(str(payload.get("ERRMSG") or "iClass 今日课程查询失败"))
            courses = []
            for item in payload["result"]:
                if (not isinstance(item, dict) or type(item.get("id")) not in (str, int)
                        or not str(item["id"]) or any(ch.isspace() for ch in str(item["id"]))):
                    raise IclassApiError("iClass 课程记录缺少有效排课 ID")
                value = item.get("signStatus")
                sign_status = int(value) if type(value) in (str, int) and str(value) in {"0", "1"} else None
                courses.append(IclassCourse(str(item["id"]), str(item.get("courseName") or ""),
                                           str(item.get("classBeginTime") or ""), str(item.get("classEndTime") or ""), sign_status))
            return courses

        return self._with_session(query)

    def sign_course(self, course_id: str) -> str:
        if not isinstance(course_id, str) or not course_id or any(ch.isspace() for ch in course_id):
            raise IclassApiError("缺少有效排课 ID，未提交签到")

        def sign():
            timestamp_url = APP_BASE + "common/get_timestamp.action" if self.use_vpn else "http://iclass.buaa.edu.cn:8081/app/common/get_timestamp.action"
            clock = self._json("GET", timestamp_url)
            if "STATUS" in clock and not self._success(clock):
                raise IclassApiError("iClass 获取服务器时间失败，未提交签到")
            timestamp = clock.get("timestamp")
            if type(timestamp) not in (str, int) or not str(timestamp).isdigit():
                raise IclassApiError("iClass 未返回有效服务器时间，未提交签到")
            url = APP_BASE + "course/stu_scan_sign.action" if self.use_vpn else "http://iclass.buaa.edu.cn:8081/eschool/app/course/stu_scan_sign.action"
            payload = self._json("POST", url, headers={"sessionId": self.session_id},
                                 params={"courseSchedId": course_id, "timestamp": str(timestamp)}, data={"id": self.user_id})
            result = payload.get("result")
            value = result.get("stuSignStatus") if isinstance(result, dict) else None
            if not self._success(payload) or type(value) not in (str, int) or str(value) != "1":
                raise IclassApiError(str(payload.get("ERRMSG") or "iClass 未确认签到成功"))
            return str(payload.get("ERRMSG") or "课堂签到成功")

        return self._with_session(sign)

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

import httpx

from .crypto import BykcCrypto
from .exceptions import BoyaApiError, SessionExpired, map_boya_error
from .models import BoyaCourse
from .logging import log_event
from .webvpn import to_webvpn_url

BYKC_BASE = "https://bykc.buaa.edu.cn/sscv"
BYKC_REFERER = "https://bykc.buaa.edu.cn/system/course-select"
BYKC_ORIGIN = "https://bykc.buaa.edu.cn"
COURSE_PAGE_SIZE = 20
COURSE_PAGE_TIMEOUT = httpx.Timeout(15, connect=5, write=5, pool=5)
READ_APIS = {"getAllConfig", "queryStudentSemesterCourseByPage", "queryCourseById", "queryChosenCourse", "queryStatisticByUserId"}
logger = logging.getLogger(__name__)


class BykcClient:
    def __init__(
        self,
        token: str,
        http_client: httpx.Client | None = None,
        use_vpn: bool = True,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.token = token
        self.client = http_client or httpx.Client(timeout=25, follow_redirects=True)
        self.use_vpn = use_vpn
        if cookies:
            restore_cookies(self.client, cookies)

    def endpoint(self, api: str) -> str:
        url = f"{BYKC_BASE}/{api}"
        return to_webvpn_url(url) if self.use_vpn else url

    def close(self) -> None:
        self.client.close()

    def upstream(self, url: str) -> str:
        return to_webvpn_url(url) if self.use_vpn else url

    def call(self, api: str, payload: dict[str, Any] | str | None = None,
             *, timeout: httpx.Timeout | None = None) -> dict[str, Any]:
        attempts = 2 if api in READ_APIS else 1
        for attempt in range(attempts):
            try:
                return self._call_once(api, payload, timeout=timeout)
            except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                if attempt + 1 == attempts:
                    raise
                reason = type(exc).__name__
            except httpx.HTTPStatusError as exc:
                if attempt + 1 == attempts or exc.response.status_code not in {502, 503, 504}:
                    raise
                reason = f"HTTP {exc.response.status_code}"
            log_event(logger, logging.WARNING, "read request retry", api=api, attempt=attempt + 1, reason=reason)
            time.sleep(0.5)
        raise RuntimeError("查询重试意外结束")

    def _call_once(self, api: str, payload: dict[str, Any] | str | None = None,
                   *, timeout: httpx.Timeout | None = None) -> dict[str, Any]:
        crypto = BykcCrypto()
        request = crypto.encrypt_request(payload or {})
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Referer": self.upstream(BYKC_REFERER),
            "Origin": self.upstream(BYKC_ORIGIN),
            "Authtoken": self.token,
            "auth_token": self.token,
            "authtoken": self.token,
            **request.headers,
        }
        request_options = {"timeout": timeout} if timeout is not None else {}
        response = self.client.post(self.endpoint(api), content=request.body, headers=headers, **request_options)
        if response.status_code == 401:
            raise SessionExpired("会话已失效，请重新登录")
        response.raise_for_status()
        try:
            decoded = crypto.decrypt_response(response.content)
        except Exception as exc:
            text = response.text[:240].replace("\n", " ")
            if looks_like_login_page(response.text):
                raise SessionExpired("WebVPN session missing or expired; run `autoboya login <user>` again") from exc
            raise BoyaApiError("decode_error", f"Unable to decrypt BYKC response: {text}") from exc
        if not isinstance(decoded, dict) or type(decoded.get("status")) not in (str, int):
            raise BoyaApiError("invalid_response", "接口响应缺少有效状态，无法确认操作结果")
        status = decoded["status"]
        errmsg = decoded.get("errmsg") or decoded.get("msg") or ""
        if status not in ("0", 0, "200", 200):
            error = map_boya_error(str(errmsg), status)
            raise error
        return decoded

    def get_all_config(self) -> dict[str, Any]:
        return self.call("getAllConfig", {})

    def query_course_page(self, page_number: int = 1, page_size: int = COURSE_PAGE_SIZE) -> dict[str, Any]:
        started = time.monotonic()
        try:
            result = self.call("queryStudentSemesterCourseByPage", {"pageNumber": page_number, "pageSize": page_size},
                               timeout=COURSE_PAGE_TIMEOUT)
        except Exception as exc:
            log_event(logger, logging.WARNING, "course page failed", page=page_number, page_size=page_size,
                      elapsed_seconds=round(time.monotonic() - started, 2), error_type=type(exc).__name__)
            raise
        log_event(logger, logging.INFO, "course page fetched", page=page_number, page_size=page_size,
                  elapsed_seconds=round(time.monotonic() - started, 2))
        return result

    def query_courses(self, page_number: int = 1, page_size: int = COURSE_PAGE_SIZE,
                      after_page: Callable[[], None] | None = None) -> list[BoyaCourse]:
        if page_number < 1 or page_size < 1:
            raise ValueError("分页编号和每页数量必须大于零")
        courses: list[BoyaCourse] = []
        current_page = page_number
        while True:
            data = self.query_course_page(current_page, page_size)
            page_data = data.get("data") if isinstance(data, dict) else None
            if not isinstance(page_data, dict) or not isinstance(page_data.get("content"), list):
                raise BoyaApiError("invalid_response", "课程分页缺少有效列表，保留原有缓存")
            content = page_data["content"]
            courses.extend(parse_course(item) for item in content)
            total_pages = parse_int(page_data.get("totalPages"))
            total_elements = parse_int(page_data.get("totalElements"))
            if after_page is not None:
                after_page()
            if page_data.get("last") is True:
                break
            if total_elements is not None and len(courses) >= total_elements:
                break
            if total_pages is not None and current_page >= total_pages:
                break
            if not content:
                if ((total_elements is not None and len(courses) < total_elements)
                        or (total_pages is not None and current_page < total_pages)):
                    raise BoyaApiError("invalid_response", "课程分页提前结束，保留原有缓存")
                break
            if total_pages is None and total_elements is None and len(content) < page_size:
                break
            current_page += 1
        if page_number == 1 and total_elements is not None and len(courses) < total_elements:
            raise BoyaApiError("invalid_response", "课程分页结果不完整，保留原有缓存")
        return courses

    def query_course_detail(self, course_id: int) -> BoyaCourse:
        data = self.call("queryCourseById", {"id": course_id})
        return parse_course((data.get("data") or {}) if isinstance(data, dict) else {})

    def query_chosen_courses(self, start_date: str, end_date: str) -> list[BoyaCourse]:
        data = self.call("queryChosenCourse", {"startDate": start_date, "endDate": end_date})
        items = (((data.get("data") or {}).get("courseList") or []) if isinstance(data, dict) else [])
        return [parse_chosen_course(item) for item in items]

    def query_statistics(self) -> dict[str, Any]:
        data = self.call("queryStatisticByUserId", {})
        return (data.get("data") or {}) if isinstance(data, dict) else {}

    def select_course(self, course_id: int) -> dict[str, Any]:
        return self.call("choseCourse", {"courseId": course_id})

    def drop_course(self, chosen_or_course_id: int) -> dict[str, Any]:
        return self.call("delChosenCourse", {"id": chosen_or_course_id})

    def sign_course(self, course_id: int, lat: float, lng: float, sign_type: int) -> dict[str, Any]:
        return self.call(
            "signCourseByUser",
            {"courseId": course_id, "signLat": lat, "signLng": lng, "signType": sign_type},
        )


def parse_course(raw: dict[str, Any]) -> BoyaCourse:
    sign_config = parse_sign_config(raw.get("courseSignConfig"))
    return BoyaCourse(
        id=int(raw.get("id") or 0),
        name=raw.get("courseName") or raw.get("name") or "",
        location=raw.get("coursePosition") or "",
        category=parse_category(raw),
        selected=bool(raw.get("selected", raw.get("isSelected", False))),
        course_start=raw.get("courseStartDate"),
        course_end=raw.get("courseEndDate"),
        select_start=raw.get("courseSelectStartDate"),
        select_end=raw.get("courseSelectEndDate"),
        cancel_end=raw.get("courseCancelEndDate"),
        current_count=parse_int(raw.get("courseCurrentCount")),
        max_count=parse_int(raw.get("courseMaxCount")),
        campus=raw.get("courseCampusList"),
        sign_config=sign_config,
        sign_type=parse_int(raw.get("courseSignType")),
        raw=raw,
    )


def parse_chosen_course(raw: dict[str, Any]) -> BoyaCourse:
    course_info = raw.get("courseInfo") if isinstance(raw, dict) else None
    if isinstance(course_info, dict):
        merged = dict(course_info)
        merged["selected"] = True
        merged["chosenCourseId"] = raw.get("id")
        for key in ["selectDate", "checkin", "score", "pass", "signInfo"]:
            if key in raw:
                merged[key] = raw[key]
        return parse_course(merged)
    return parse_course(raw)


def parse_category(raw: dict[str, Any]) -> str:
    kind = raw.get("courseNewKind2")
    if isinstance(kind, dict):
        return kind.get("kindName") or ""
    return raw.get("courseKind") or raw.get("kindName") or ""


def parse_sign_config(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    text = raw.replace('\\"', '"')
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def parse_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def restore_cookies(client: httpx.Client, cookies: list[dict[str, Any]]) -> None:
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        client.cookies.set(
            name,
            value,
            domain=cookie.get("domain") if isinstance(cookie.get("domain"), str) else None,
            path=cookie.get("path") if isinstance(cookie.get("path"), str) else "/",
        )


def looks_like_login_page(text: str) -> bool:
    head = text[:1000].lower()
    return "<html" in head and ("cas login" in head or "loginform" in head or "sso.buaa.edu.cn" in head)

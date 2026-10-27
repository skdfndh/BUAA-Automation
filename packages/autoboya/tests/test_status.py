from datetime import datetime
from io import StringIO

import pytest
from rich.console import Console
from typer.testing import CliRunner

from autoboya import status
from autoboya.cli import app
from autoboya.config import LOG_FILE, RUN_LOCK_FILE
from autoboya.storage import AutoBoyaStore


@pytest.fixture
def store(tmp_path):
    return AutoBoyaStore(tmp_path / "data")


def write_log(store, text):
    path = store.path(LOG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_status_does_not_create_data_directory(store, monkeypatch):
    monkeypatch.setattr(status, "task_state", lambda: "未找到自动启动任务")
    output = StringIO()
    status.show_status(store, Console(file=output, width=140))
    assert not store.root.exists()
    assert "未运行" in output.getvalue()
    assert "暂无后台操作记录" in output.getvalue()


def test_stale_pid_does_not_mean_worker_is_running(store):
    store.path("run").mkdir(parents=True)
    store.path("run/autoboya.pid").write_text("999999")
    assert status.worker_running(store) is False


def test_reads_existing_worker_lock_without_modifying_files(store):
    path = store.path(RUN_LOCK_FILE)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"0")
    before = path.stat().st_mtime_ns
    with store.lock(RUN_LOCK_FILE):
        assert status.worker_running(store) is True
        assert path.stat().st_mtime_ns == before
    assert status.worker_running(store) is False
    assert path.read_bytes() == b"0"


def test_action_types_results_errors_and_heartbeat_are_separate(store):
    write_log(store, """2026-10-27 12:00:00,000 INFO automation heartbeat user=243*** auto_boya_check=0
2026-10-27 12:01:00,000 INFO automation action user=243*** action=select course_id=1 ok=True reason=selected
2026-10-27 12:02:00,000 INFO automation action user=243*** action=sign course_id=1 ok=True reason=signed
2026-10-27 12:03:00,000 WARNING automation action user=243*** action=signout course_id=1 ok=False reason=窗口关闭
2026-10-27 12:04:00,000 ERROR automation refresh failed error=网络超时
""")
    report = status.collect_status(store)
    assert report["heartbeat"] == "2026-10-27 12:00:00"
    assert report["checks"] == 0
    assert [row[2] for row in report["outcomes"]] == ["signout", "sign", "select"]
    assert [row[4] for row in report["outcomes"]] == ["失败", "成功", "成功"]
    assert len(report["errors"]) == 2
    assert "网络超时" in report["errors"][0][1]


def test_older_successes_are_read_from_journal_and_users_are_masked(store):
    store.save_json("cache/action_journal.json", {"2026-10-20:24312345:sign:42": "signed"})
    report = status.collect_status(store)
    assert report["outcomes"] == [("2026-10-20", "243***", "sign", "42", "成功", "signed")]
    assert "24312345" not in str(report)


def test_journal_does_not_duplicate_logged_success(store):
    write_log(store, "2026-10-27 12:00:00,000 INFO automation action user=243*** action=sign course_id=42 ok=True reason=signed\n")
    store.save_json("cache/action_journal.json", {"2026-10-27:24312345:sign:42": "signed"})
    assert len(status.collect_status(store)["outcomes"]) == 1


def test_corrupt_cache_is_reported_without_hiding_log(store):
    write_log(store, "2026-10-27 12:00:00,000 ERROR automation loop failed error=示例错误\n")
    store.path("cache").mkdir()
    store.path("cache/courses.json").write_text("invalid-json")
    report = status.collect_status(store)
    assert report["errors"]
    assert "cache/courses.json" in report["issues"][0]


def test_recent_log_read_is_bounded_and_keeps_complete_utf8_lines(store, monkeypatch):
    write_log(store, "旧记录\n" * 100 + "2026-10-27 12:00:00,000 ERROR 中文错误\n")
    monkeypatch.setattr(status, "LOG_LIMIT", 100)
    lines = status.recent_log_lines(store)
    assert lines[-1].endswith("中文错误")
    assert "�" not in "".join(lines)
    assert len(lines) < 100


def test_report_redacts_credentials(store):
    write_log(store, "2026-10-27 12:00:00,000 ERROR request failed token=private-token password=private-password\n")
    report = status.collect_status(store)
    assert "private-token" not in str(report)
    assert "private-password" not in str(report)


def test_running_worker_with_old_heartbeat_is_flagged(store):
    write_log(store, "2026-10-27 12:00:00,000 INFO automation heartbeat user=243*** auto_boya_check=0\n")
    with store.lock(RUN_LOCK_FILE):
        report = status.collect_status(store, now=datetime(2026, 10, 27, 12, 10))
    assert any("超过 5 分钟" in issue for issue in report["issues"])


def test_interactive_view_exits_without_changing_runtime_files(store, monkeypatch):
    monkeypatch.setenv("AUTOBOYA_HOME", str(store.root))
    monkeypatch.setattr(status, "task_state", lambda: "运行中")
    result = CliRunner().invoke(app, ["status", "--interactive"], input="q\n")
    assert result.exit_code == 0
    assert "按回车刷新" in result.output
    assert not store.root.exists()


def test_persistently_disabled_course_is_visible_even_without_recent_log(store):
    store.save_json("cache/action_journal.json", {"24312345:select_failed:42": {
        "attempts": 3, "disabled": True, "message": "选课冲突",
    }})
    report = status.collect_status(store)
    assert any("243*** 的课程 42 已停止自动选课重试" in issue for issue in report["issues"])
    assert "24312345" not in str(report)


def test_invalid_heartbeat_does_not_crash_viewer(store):
    write_log(store, "2026-99-27 12:00:00,000 INFO automation heartbeat user=243*** auto_boya_check=0\n")
    with store.lock(RUN_LOCK_FILE):
        report = status.collect_status(store)
    assert any("时间格式不正确" in issue for issue in report["issues"])


def test_class_signin_uses_distinct_results_heartbeat_and_course_names(store, monkeypatch):
    write_log(store, "2026-10-27 10:00:00,000 INFO classroom heartbeat user=243*** courses=1 auto_class_check=1\n"
                     "2026-10-27 10:00:01,000 INFO automation action user=243*** action=class_sign course_id=42 ok=True reason=课堂签到成功\n")
    store.save_json("settings.json", {"class_signin_enabled": True})
    store.save_json("cache/courses.json", [{"id": 42, "courseName": "同 ID 的博雅课"}])
    store.save_json("cache/iclass.json", {"24312345": {"date": "2026-10-27", "courses": [
        {"id": "42", "name": "普通课堂", "begin": "09:50", "end": "11:25", "sign_status": 1},
    ]}})
    monkeypatch.setattr(status, "task_state", lambda: "运行中")
    report = status.collect_status(store, now=datetime(2026, 10, 27, 10, 1))
    assert report["class_signin_enabled"]
    assert report["heartbeat"] == "暂无"
    assert report["class_heartbeat"] == "2026-10-27 10:00:00"
    assert report["outcomes"][0][2] == "class_sign"
    output = StringIO()
    status.show_status(store, Console(file=output, width=180), now=datetime(2026, 10, 27, 10, 1))
    assert "普通课堂" in output.getvalue()
    assert "同 ID 的博雅课" not in output.getvalue()


def test_stale_course_cache_is_reported_separately_from_worker_liveness(store):
    import os

    store.save_json("cache/courses.json", [{"id": 42}])
    updated = datetime(2026, 10, 27, 10).timestamp()
    os.utime(store.path("cache/courses.json"), (updated, updated))
    with store.lock(RUN_LOCK_FILE):
        report = status.collect_status(store, now=datetime(2026, 10, 27, 10, 30))
    assert report["running"]
    assert report["course_count"] == 1
    assert report["course_cache_updated"] == "2026-10-27 10:00:00"
    assert any("超过 15 分钟" in issue for issue in report["issues"])

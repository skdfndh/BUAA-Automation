from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import ACTION_JOURNAL_FILE, ICLASS_CACHE_FILE, RUN_LOCK_FILE, SECRETS_FILE, SELECTED_CACHE_FILE, SETTINGS_FILE, STATISTICS_CACHE_FILE, USERS_FILE, app_dir
from .models import UserRecord
from .exceptions import OperationInProgress


class AutoBoyaStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or app_dir()

    def path(self, relative: str) -> Path:
        return self.root / relative

    @contextmanager
    def lock(self, relative: str):
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, PermissionError) as exc:
                raise OperationInProgress("自动化正在运行或启动，请勿重复执行") from exc
            try:
                yield
            finally:
                # 锁文件保持原位，避免其他进程锁住不同文件而同时进入。
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def init(self) -> None:
        for relative in ["cache", "logs", "run", "captcha", "sessions"]:
            self.path(relative).mkdir(parents=True, exist_ok=True)
        if not self.path(USERS_FILE).exists():
            self.save_json(USERS_FILE, [])
        if not self.path(SETTINGS_FILE).exists():
            self.save_json(SETTINGS_FILE, {"auto_select_mode": "autonomous_sign_only", "auto_select_categories": []})

    def load_json(self, relative: str, default: Any = None) -> Any:
        path = self.path(relative)
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def save_json(self, relative: str, value: Any, mode: int | None = None) -> None:
        path = self.path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        if mode is not None:
            os.chmod(path, mode)

    def load_users(self) -> list[dict[str, Any]]:
        return list(self.load_json(USERS_FILE, []))

    def save_users(self, users: list[dict[str, Any]]) -> None:
        self.save_json(USERS_FILE, users)

    def user_records(self) -> list[UserRecord]:
        return [UserRecord(**{**item, "campus": item.get("campus") or "北京"}) for item in self.load_users()]

    def get_user(self, username: str) -> UserRecord | None:
        for user in self.user_records():
            if user.username == username:
                return user
        return None

    def upsert_user(self, user: UserRecord) -> None:
        users = [item for item in self.user_records() if item.username != user.username]
        users.append(user)
        users.sort(key=lambda record: record.username)
        self.save_users([asdict(item) for item in users])

    def remove_user(self, username: str) -> bool:
        if "/" in username or "\\" in username:
            raise ValueError("账号不能包含路径分隔符")
        with self.lock(RUN_LOCK_FILE):
            user = self.get_user(username)
            if user is None:
                return False
            if user.password_ref == "keyring":
                import keyring

                # 钥匙串删除失败时保留账号记录，让操作者能够明确重试。
                if keyring.get_password("autoboya", username) is not None:
                    keyring.delete_password("autoboya", username)
            secrets = self.load_json(SECRETS_FILE, {})
            if username in secrets:
                secrets.pop(username)
                self.save_json(SECRETS_FILE, secrets, mode=0o600)
            self.path(f"sessions/{username}.json").unlink(missing_ok=True)
            for relative in (SELECTED_CACHE_FILE, STATISTICS_CACHE_FILE, ICLASS_CACHE_FILE):
                values = self.load_json(relative, {})
                if username in values:
                    values.pop(username)
                    self.save_json(relative, values)
            journal = self.load_json(ACTION_JOURNAL_FILE, {})
            journal = {key: value for key, value in journal.items()
                       if f":{username}:" not in key and not key.startswith(f"{username}:select_failed:")}
            self.save_json(ACTION_JOURNAL_FILE, journal)
            self.save_users([asdict(item) for item in self.user_records() if item.username != username])
            return True

    def save_unsafe_password(self, username: str, password: str) -> None:
        secrets = self.load_json(SECRETS_FILE, {})
        secrets[username] = password
        self.save_json(SECRETS_FILE, secrets, mode=0o600)

    def load_unsafe_password(self, username: str) -> str | None:
        secrets = self.load_json(SECRETS_FILE, {})
        value = secrets.get(username)
        return value if isinstance(value, str) else None


def try_store_keyring_password(username: str, password: str) -> bool:
    try:
        import keyring

        keyring.set_password("autoboya", username, password)
        return True
    except Exception:
        return False


def try_get_keyring_password(username: str) -> str | None:
    try:
        import keyring

        return keyring.get_password("autoboya", username)
    except Exception:
        return None

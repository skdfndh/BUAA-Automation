"""学校登录需要人工处理时，在当前 Windows 桌面提醒。"""

import hashlib
import logging
import os
from pathlib import Path
import subprocess
import sys
import time

from .exceptions import OperationInProgress
from .logging import mask_username
from .session import login_and_save
from .storage import AutoBoyaStore


def session_version(store, username):
    try:
        return store.path(f"sessions/{username}.json").stat().st_mtime_ns
    except FileNotFoundError:
        return None


class LoginReminder:
    def __init__(self, store):
        self.store = store
        self.windows = {}
        self.snoozed = {}

    def notify(self, username, version):
        now = time.monotonic()
        process = self.windows.get(username)
        if process is not None:
            if process.poll() is None:
                return
            del self.windows[username]
            if process.returncode:
                logging.getLogger(__name__).warning("登录提醒进程异常退出，退出码 %s", process.returncode)
            old_version, _ = self.snoozed[username]
            self.snoozed[username] = (old_version, now + 600)
        if username in self.snoozed:
            previous, until = self.snoozed[username]
            if version == previous and now < until:
                return
        self.snoozed[username] = (version, now + 600)
        try:
            self.windows[username] = subprocess.Popen(
                [sys.executable, "-m", "autoboya.login_reminder"],
                cwd=Path(__file__).resolve().parent.parent,
                env={**os.environ, "AUTOBOYA_HOME": str(self.store.root.resolve()),
                     "AUTOBOYA_LOGIN_USER": username, "AUTOBOYA_LOGIN_VERSION": str(version)},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            logging.getLogger(__name__).warning("无法打开学校登录提醒，稍后将重试")


def show_window(store, username, version):
    import tkinter as tk
    from tkinter import simpledialog, ttk

    def still_needed():
        user = store.get_user(username)
        return user and user.enabled and str(session_version(store, username)) == version

    if not still_needed():
        return
    root = tk.Tk()
    root.title("博雅和普通课需要重新登录")
    root.attributes("-topmost", True)
    message = tk.StringVar(value=f"学校登录已失效（{mask_username(username)}）。请重新登录，以便后台继续检查签到。")
    ttk.Label(root, textvariable=message, wraplength=400, padding=18).pack()

    class CaptchaDialog(simpledialog.Dialog):
        def __init__(self, image_path):
            self.image_path = image_path
            super().__init__(root, title="请输入学校验证码")

        def body(self, frame):
            try:
                from PIL import Image, ImageTk
                with Image.open(self.image_path) as source:
                    self.photo = ImageTk.PhotoImage(source)
                ttk.Label(frame, image=self.photo).pack()
            except ImportError:
                os.startfile(self.image_path)
            ttk.Label(frame, text="请输入图片中的学校验证码").pack()
            self.entry = ttk.Entry(frame)
            self.entry.pack()
            return self.entry

        def apply(self):
            self.result = self.entry.get().strip()

    def captcha(challenge):
        return CaptchaDialog(challenge.image_path).result

    def login():
        if not still_needed():
            root.destroy()
            return
        button.configure(state="disabled")
        message.set("正在连接学校认证，请稍候。")
        root.update_idletasks()
        try:
            login_and_save(store, username, captcha_provider=captcha)
        except Exception:
            message.set("登录尚未完成。请检查网络，点击重新登录获取新的验证码。")
            button.configure(state="normal")
            return
        message.set("登录成功，后台将在下一次扫描时恢复。")
        root.after(2500, root.destroy)

    def check_session():
        if str(button["state"]) != "disabled" and not still_needed():
            root.destroy()
        else:
            root.after(5000, check_session)

    button = ttk.Button(root, text="重新登录", command=login)
    button.pack(pady=5)
    ttk.Button(root, text="稍后提醒", command=root.destroy).pack(pady=8)
    root.after(5000, check_session)
    root.bell()
    root.mainloop()


if __name__ == "__main__":
    store = AutoBoyaStore()
    username = os.environ["AUTOBOYA_LOGIN_USER"]
    account_key = hashlib.sha256(username.encode()).hexdigest()
    try:
        # 后台重启时，旧窗口仍可能在桌面等待输入。
        with store.lock(f"run/login-reminder-{account_key}.lock"):
            show_window(store, username, os.environ["AUTOBOYA_LOGIN_VERSION"])
    except OperationInProgress:
        pass

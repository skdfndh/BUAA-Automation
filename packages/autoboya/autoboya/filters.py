from __future__ import annotations

import logging
from collections import Counter

from .cache import CourseCache
from .config import SETTINGS_FILE
from .logging import log_event
from .rules import auto_select_categories
from .storage import AutoBoyaStore

logger = logging.getLogger(__name__)


def load_auto_select_categories(store: AutoBoyaStore) -> frozenset[str]:
    try:
        return auto_select_categories(store.load_json(SETTINGS_FILE, {}))
    except (OSError, ValueError, TypeError) as exc:
        # 无法确认勾选范围时暂停新选课，不阻断已有课程的签到。
        log_event(logger, logging.WARNING, "auto select filters unreadable", error=exc)
        return frozenset()


def save_auto_select_categories(store: AutoBoyaStore, categories: list[str]) -> None:
    if not isinstance(categories, list) or any(not isinstance(value, str) or not value.strip() for value in categories):
        raise ValueError("课程类别必须为非空名称列表")
    settings = store.load_json(SETTINGS_FILE, {})
    if not isinstance(settings, dict):
        raise ValueError("设置文件格式不正确，请先修复设置文件")
    settings["auto_select_categories"] = sorted(set(categories))
    store.save_json(SETTINGS_FILE, settings)


class FilterWindow:
    def __init__(self, root, store: AutoBoyaStore) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.store = store
        root.title("博雅选课筛选")
        root.minsize(620, 520)
        style = ttk.Style(root)
        style.configure(".", font=("Microsoft YaHei UI", 11))
        style.configure("TCheckbutton", padding=(2, 4))
        style.configure("TButton", padding=(8, 4))
        root.columnconfigure(0, weight=1)
        frame = ttk.Frame(root, padding=22)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="自动选课 · 课程类别", font=("Microsoft YaHei UI", 15, "bold")).grid(sticky="w")
        ttk.Label(frame, text="只选择勾选的类别；适用于所有已启用账号。", padding=(0, 8, 0, 12)).grid(sticky="w")

        saved = load_auto_select_categories(store)
        counts = Counter(course.category for course in CourseCache(store).parsed_courses() if course.category)
        choices = sorted(set(counts) | saved)
        self.variables = {name: tk.BooleanVar(value=name in saved and name != "其他方面") for name in choices}
        box = ttk.LabelFrame(frame, text="勾选允许自动选择的类别", padding=12)
        box.grid(sticky="ew")
        for name, variable in self.variables.items():
            label = f"{name}（缓存 {counts[name]} 门）"
            if name == "其他方面":
                label += " · 原有规则排除"
            ttk.Checkbutton(box, text=label, variable=variable,
                            state="disabled" if name == "其他方面" else "normal",
                            command=self.mark_changed).pack(anchor="w", pady=4)
        if not choices:
            ttk.Label(box, text="暂无类别，请刷新课程缓存后重新打开窗口。").pack(anchor="w")

        buttons = ttk.Frame(frame, padding=(0, 12))
        buttons.grid(sticky="w")
        ttk.Button(buttons, text="全选可选类别", command=lambda: self.set_all(True)).pack(side="left")
        ttk.Button(buttons, text="全不选", command=lambda: self.set_all(False)).pack(side="left", padx=8)
        ttk.Label(frame, text="保留条件：自主签到、选课时间、容量、账号校区。\n"
                  "未配置或全不选会暂停新选课；已选课程的签到、签退照常。\n"
                  "新出现的类别默认不选。手动命令选课不受此设置影响。",
                  wraplength=525, padding=(0, 0, 0, 12)).grid(sticky="w")
        self.message = tk.StringVar(value=self.saved_message(saved))
        ttk.Label(frame, textvariable=self.message, wraplength=525).grid(sticky="w", pady=(0, 12))
        ttk.Button(frame, text="保存并生效", command=self.save).grid(sticky="e")
        root.bind("<Control-s>", lambda event: self.save())

    @staticmethod
    def saved_message(categories: frozenset[str]) -> str:
        active = sorted(categories - {"其他方面"})
        return "当前已保存：" + ("、".join(active) if active else "未勾选，暂停新选课")

    def mark_changed(self) -> None:
        self.message.set("有未保存的修改。点击“保存并生效”后后台按新类别执行。")

    def set_all(self, checked: bool) -> None:
        for name, variable in self.variables.items():
            variable.set(checked and name != "其他方面")
        self.mark_changed()

    def save(self) -> None:
        from tkinter import messagebox

        categories = [name for name, variable in self.variables.items() if variable.get() and name != "其他方面"]
        try:
            save_auto_select_categories(self.store, categories)
        except (OSError, ValueError, TypeError) as exc:
            messagebox.showerror("保存失败", f"设置未保存：{exc}")
            return
        self.message.set("保存成功，无需重启后台。\n" + self.saved_message(frozenset(categories)))


def show_filter_window(store: AutoBoyaStore) -> None:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        FilterWindow(root, store)
    except (OSError, ValueError, TypeError) as exc:
        messagebox.showerror("无法打开筛选", f"读取本地配置或课程缓存失败：{exc}", parent=root)
        root.destroy()
        return
    root.deiconify()
    root.mainloop()

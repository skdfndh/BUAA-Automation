from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from auto_td.models import User
from auto_td.storage import AppStorage
from autoboya.storage import AutoBoyaStore


def configure(args):
    store = AutoBoyaStore()
    if not store.get_user(args.user) or not store.get_user(args.user).enabled:
        raise ValueError("账号必须先加入博雅启用账号")
    if args.confirmed_count < 0 or args.target_count <= 0 or not args.term.strip():
        raise ValueError("学期标识或次数无效")
    date.fromisoformat(args.start_date)
    if args.end_date and date.fromisoformat(args.end_date) < date.fromisoformat(args.start_date):
        raise ValueError("结束日期早于启用日期")
    photos = [Path(args.entrance_image).expanduser().resolve(), Path(args.exit_image).expanduser().resolve()]
    if any(not path.is_file() for path in photos):
        raise ValueError("入口及出口图片必须是现有文件")
    if not args.server.strip() or not 1 <= args.port <= 65535:
        raise ValueError("服务器或端口无效")
    storage = AppStorage(store.path("td"))
    if storage.users_path.exists() and storage.load_users():
        raise ValueError("已有 TD 用户，拒绝覆盖配置和进度")
    if storage.state_path.exists() and (storage.load_state().get("daily_runs") or storage.load_state().get("counts")):
        raise ValueError("已有 TD 次数或进度，拒绝覆盖")
    user = User.from_dict({"student_id": args.user, "entrance_machine_id": args.entrance,
                          "exit_machine_id": args.exit, "entrance_image": "entrance" + photos[0].suffix.lower(),
                          "exit_image": "exit" + photos[1].suffix.lower(), "rounds": 3,
                          "wait_time_min": 180, "wait_time_max": 200})
    if not ((args.entrance in {2, 3, 4} and args.exit in {5, 6, 7}) or
            (args.entrance in {8, 9, 10} and args.exit in {11, 12, 13})):
        raise ValueError("入口和出口必须为同一校区的机位组合")
    storage.initialize()
    config = storage.load_config()
    config["server"].update(ip=args.server.strip(), port=args.port)
    storage.save_config(config)
    settings = storage.load_settings()
    settings["schedule"]["poll_seconds"] = 15
    settings["integration"] = {"enabled": True, "term_id": args.term, "start_date": args.start_date,
                               "end_date": args.end_date, "target_count": args.target_count}
    storage.save_settings(settings)
    for source, name in zip(photos, (user.entrance_image, user.exit_image)):
        target = storage.image_path(name)
        if target.exists():
            if target.read_bytes() != source.read_bytes():
                raise ValueError("目标图片已存在且内容不同，拒绝覆盖")
        else:
            storage.add_image(source, name=name)
    storage.save_user(user)
    storage.set_last_count(user.student_id, args.confirmed_count, count_source="user-confirmed")
    # 最后才打开调度，避免半配置时被已有后台读到。
    current = store.load_json("settings.json", {})
    current["td_enabled"] = True
    store.save_json("settings.json", current)


def parser():
    result = argparse.ArgumentParser(description="配置新的 TD 账号，拒绝覆盖已有进度")
    result.add_argument("--user", required=True)
    result.add_argument("--server", required=True)
    result.add_argument("--port", type=int, default=8888)
    result.add_argument("--term", required=True)
    result.add_argument("--start-date", required=True)
    result.add_argument("--end-date")
    result.add_argument("--confirmed-count", type=int, required=True)
    result.add_argument("--target-count", type=int, default=32)
    result.add_argument("--entrance", type=int, default=2)
    result.add_argument("--exit", type=int, default=6)
    result.add_argument("--entrance-image", required=True)
    result.add_argument("--exit-image", required=True)
    return result


def main():
    arguments = parser()
    args = arguments.parse_args()
    try:
        configure(args)
    except (OSError, ValueError, KeyError) as exc:
        arguments.error(str(exc))
    print("TD configured; telemetry disabled; existing Boya settings preserved")


if __name__ == "__main__":
    main()

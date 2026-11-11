from __future__ import annotations


def remaining_slots(stats: object, category: str, reservations: list[int]) -> int:
    statistical = stats.get("statistical") if isinstance(stats, dict) else None
    if not isinstance(statistical, dict):
        raise ValueError("无法读取学校课程要求，暂停新选课")
    rows = [row for group in statistical.values() if isinstance(group, dict)
            for key, row in group.items() if str(key).split("|", 1)[-1] == category]
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("课程类别要求不唯一或缺失，暂停新选课")
    row = rows[0]
    counts = [row.get(key) for key in ("assessmentCount", "completeAssessmentCount", "failAssessmentCount",
                                      "undoneAssessmentCount")]
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("课程要求或完成门数无效，暂停新选课")
    courses = row.get("courseUserList")
    if not isinstance(courses, list):
        raise ValueError("缺少学校已选明细，暂停新选课")
    observed = set()
    for item in courses:
        info = item.get("courseInfo") if isinstance(item, dict) else None
        if not isinstance(info, dict) or type(info.get("id")) is not int:
            raise ValueError("学校已选明细格式无效，暂停新选课")
        observed.add(info["id"])
    # 请求超时或统计延迟时仍占用名额，避免重启后继续选出多门。
    unconfirmed = len(set(reservations) - observed)
    required, completed, failed, pending = counts
    # 完成为通过门数，未通过还要另扣一次；负的有效门数不能在计算缺额前归零。
    return max(0, required - (completed - failed) - pending - unconfirmed)

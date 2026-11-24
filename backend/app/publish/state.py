TRANSITIONS: dict[str, dict[str, str]] = {
    "draft": {"submit": "prechecking", "export": "exporting", "cancel": "cancelled"},
    "prechecking": {"pass": "awaiting_review", "fail": "precheck_failed"},
    "precheck_failed": {"submit": "prechecking", "cancel": "cancelled"},
    "awaiting_review": {
        "approve": "approved",
        "reject": "draft",
        "export": "exporting",
        "cancel": "cancelled",
    },
    "approved": {
        "publish": "publishing",
        "export": "exporting",
        "cancel": "cancelled",
        "fail": "publish_failed",
    },
    "exporting": {"export_done": "exported", "fail": "export_failed"},
    "exported": {},
    # 导出失败只能重试导出，绝不能绕过人工审核到达 approved
    "export_failed": {"retry": "exporting", "cancel": "cancelled"},
    "publishing": {"succeed": "published", "fail": "publish_failed"},
    "published": {},
    "publish_failed": {"retry": "approved", "cancel": "cancelled"},
    "cancelled": {"reopen": "draft"},
}


class InvalidTransition(ValueError):
    pass


def next_status(current: str, action: str) -> str:
    target = TRANSITIONS.get(current, {}).get(action)
    if target is None:
        raise InvalidTransition(f"状态 {current} 不允许动作 {action}")
    return target

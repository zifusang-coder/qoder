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
    "approved": {"publish": "publishing", "export": "exporting", "cancel": "cancelled"},
    "exporting": {"export_done": "exported", "fail": "failed"},
    "publishing": {"succeed": "published", "fail": "failed"},
    "failed": {"retry": "approved", "cancel": "cancelled"},
}


class InvalidTransition(ValueError):
    pass


def next_status(current: str, action: str) -> str:
    target = TRANSITIONS.get(current, {}).get(action)
    if target is None:
        raise InvalidTransition(f"状态 {current} 不允许动作 {action}")
    return target

"""进程内指标计数器与 Prometheus 文本渲染。

不引入 prometheus_client：当前只需要请求量与运行失败计数，进程内字典足够，
多副本部署时由各副本分别暴露，聚合交给抓取端。
"""

import threading
from collections import Counter

_lock = threading.Lock()
_requests: Counter[tuple[str, str, int]] = Counter()
_failures: Counter[tuple[str, str]] = Counter()


def reset() -> None:
    with _lock:
        _requests.clear()
        _failures.clear()


def observe_request(method: str, path: str, status: int) -> None:
    with _lock:
        _requests[(method, path, status)] += 1


def observe_failure(kind: str, code: str) -> None:
    with _lock:
        _failures[(kind, code or "unknown")] += 1


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_metrics() -> str:
    with _lock:
        requests = sorted(_requests.items())
        failures = sorted(_failures.items())
    lines = [
        "# HELP newmedia_http_requests_total HTTP 请求计数",
        "# TYPE newmedia_http_requests_total counter",
    ]
    for (method, path, status), count in requests:
        lines.append(
            "newmedia_http_requests_total"
            f'{{method="{_escape(method)}",path="{_escape(path)}",'
            f'status="{status}"}} {count}'
        )
    lines.extend(
        [
            "# HELP newmedia_run_failures_total Agent/工作流/团队/媒体运行失败计数",
            "# TYPE newmedia_run_failures_total counter",
        ]
    )
    for (kind, code), count in failures:
        lines.append(
            "newmedia_run_failures_total"
            f'{{kind="{_escape(kind)}",code="{_escape(code)}"}} {count}'
        )
    return "\n".join(lines) + "\n"

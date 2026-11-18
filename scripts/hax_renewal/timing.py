"""为续期等待和恢复操作保留账号收尾时间。"""

import os
from time import monotonic


def remaining_time(limit: float) -> float:
    """返回当前操作可以使用的时间，上限由账号运行入口提供。"""
    deadline = os.environ.get("HAX_OPERATION_DEADLINE")
    return max(0, min(limit, float(deadline) - monotonic())) if deadline else limit


def require_time(limit: float = 1) -> float:
    """返回本次调用可用的时间，账号预算耗尽时停止。"""
    available = remaining_time(limit)
    if available <= 0:
        raise TimeoutError("hax_operation_deadline")
    return available

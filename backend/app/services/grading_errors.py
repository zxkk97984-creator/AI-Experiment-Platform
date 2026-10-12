"""判题/评分写入的共享错误类型。

``LostClaimError`` 表示"本次运行已失去认领"——恢复机制已把任务重新排队并由别的
Worker 接管，或本会话从未持有认领。任何结果写入都必须放弃，且**不得**走失败写入
（失败写入同样带栅栏条件，只会被再次拒绝，并掩盖真实原因）。
"""

from __future__ import annotations


class LostClaimError(RuntimeError):
    """失去认领资格：结果必须丢弃，由恢复机制重新调度。"""

    def __init__(self, message: str = "判题/评分认领已失效，结果丢弃"):
        super().__init__(message)


class RelatedWriteError(RuntimeError):
    """与主结果同事务的关联写入失败（目标不存在或约束冲突）。

    整个事务必须回滚：主结果不得落库，由上层走有界错误处理（重试或转人工），
    绝不允许出现"评分主记录已终态、关联正式分却是旧值"的分裂状态。
    """

"""练习榜单/统计面的请求模型（批次 6）。放自有模块，避免动共享的 schemas 注册面。"""
from __future__ import annotations

from pydantic import BaseModel


class PracticeOptInUpdate(BaseModel):
    """全平台榜加入/退出：显式布尔，不做「缺省即加入」的兜底（隐私默认关闭）。"""

    opt_in: bool

# -*- coding: utf-8 -*-
"""稳定随机源：DEMO_SEED + 实体稳定 key 派生子随机源（评审 4）。

任何随机决策（姓名、成绩、提交时间、测试通过率、AI 文本）都必须通过
make_rng(*parts) 取一个以实体稳定 key 派生的 random.Random 实例，
绝不在模块级共享一个顺序消费的随机流——这样即使部分数据已存在被跳过，
其他实体的随机序列也不会漂移。
实体 key 使用用户名、题目序号/标题等语义值，不使用数据库自增 ID；
MySQL 的 DELETE 重置后 ID 会延续，但演示答案、成绩及复核状态应保持一致。
"""
from __future__ import annotations

import random

from .constants import DEMO_SEED


def make_rng(*parts) -> random.Random:
    """按稳定 key 派生独立随机源。

    key = f"{DEMO_SEED}:{':'.join(str(p) for p in parts)}"
    同一 key 永远得到同一序列；不同实体互不影响。
    """
    key = str(DEMO_SEED) + ":" + ":".join(str(p) for p in parts)
    return random.Random(key)


def stable_choice(rng: random.Random, seq):
    """等价 rng.choice，但防御空序列。"""
    if not seq:
        raise ValueError("stable_choice 不能从空序列选择")
    return rng.choice(seq)

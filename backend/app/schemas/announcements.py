"""公告请求/响应模型——纯文本内容，scope 决定可见范围"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class AnnouncementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=2000)
    priority: Literal["normal", "important", "urgent"] = "normal"
    scope: Literal["global", "course", "offering"]
    course_id: int | None = None
    offering_id: int | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_course_scope(self):
        if self.scope == "course" and self.course_id is None:
            raise ValueError("课程公告必须指定 course_id")
        if self.scope == "global" and self.course_id is not None:
            raise ValueError("全局公告不能指定 course_id")
        if self.scope == "offering" and self.offering_id is None:
            raise ValueError("班级公告必须指定授课实例")
        if self.scope != "offering" and self.offering_id is not None:
            raise ValueError("非班级公告不可指定授课实例")
        return self

    @field_validator("expires_at")
    @classmethod
    def reject_naive_expires_at(cls, value):
        # naive datetime 与 aware 服务端时间比较会产生 TypeError，明确拒绝
        if value is not None and value.tzinfo is None:
            raise ValueError("expires_at 必须包含时区（如 2026-08-08T00:00:00Z）")
        return value


class AnnouncementRead(BaseModel):
    id: int
    title: str
    content: str
    priority: str
    scope: str
    course_id: int | None
    course_title: str | None
    author_name: str
    published_at: datetime
    expires_at: datetime | None
    is_read: bool

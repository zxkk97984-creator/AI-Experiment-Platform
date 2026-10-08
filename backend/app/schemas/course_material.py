"""Course materials expose business metadata rather than storage paths."""
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Category = Literal["lecture", "notebook", "tool", "video"]


def browser_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    parts = urlsplit(value)
    if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password
            or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("外链仅支持无账号密码的 HTTP(S) 地址")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("外链端口无效") from exc
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaterialCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    chapter_id: int | None = Field(default=None, gt=0)
    category: Category = "lecture"
    source: Literal["file", "external"] = "file"
    external_url: str | None = Field(default=None, max_length=2000)
    order_index: int = Field(default=0, ge=0, le=100000)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        if not value.strip():
            raise ValueError("资料标题不能为空")
        return value.strip()

    _url = field_validator("external_url")(browser_url)

    @model_validator(mode="after")
    def source_valid(self):
        if (self.source == "external") != bool(self.external_url):
            raise ValueError("官网链接需要 HTTP(S) 地址；文件资料不能同时设置外链")
        return self


class MaterialUpdate(StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    chapter_id: int | None = Field(default=None, gt=0)
    category: Category | None = None
    external_url: str | None = Field(default=None, max_length=2000)
    order_index: int | None = Field(default=None, ge=0, le=100000)
    status: Literal["draft", "published"] | None = None

    _url = field_validator("external_url")(browser_url)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        if value is not None and not value.strip():
            raise ValueError("资料标题不能为空")
        return value.strip() if value is not None else value


class MaterialRead(BaseModel):
    id: int
    course_id: int
    chapter_id: int | None
    title: str
    description: str | None
    category: str
    source: str
    status: str
    external_url: str | None
    order_index: int
    version: int
    filename: str | None = None
    content_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    previewable: bool = False
    can_edit: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    history: list[dict] = Field(default_factory=list)


class MaterialListRead(BaseModel):
    items: list[MaterialRead]
    page: int
    page_size: int
    total: int
    can_edit: bool
    ordinary_max_bytes: int
    large_max_bytes: int


class MaterialAccessRead(BaseModel):
    url: str
    expires_at: datetime | None = None
    external: bool = False
    previewable: bool = False


class ChapterActivityCreate(StrictModel):
    chapter_id: int = Field(gt=0)
    target_type: Literal["assignment", "exam", "lesson", "module"]
    target_id: int = Field(gt=0)
    order_index: int = Field(default=0, ge=0, le=100000)


class ChapterActivityRead(BaseModel):
    id: int
    offering_id: int
    chapter_id: int
    target_type: str
    target_id: int
    title: str
    status: str
    route: str
    order_index: int


class ChapterActivityTargetRead(BaseModel):
    target_type: str
    target_id: int
    title: str
    status: str

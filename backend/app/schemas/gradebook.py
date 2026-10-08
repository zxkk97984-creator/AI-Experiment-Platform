from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ColumnConfig(StrictModel):
    key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    title: str = Field(min_length=1, max_length=120)
    weight: Decimal = Field(ge=0, le=100, decimal_places=4)
    source_kind: Literal['manual', 'assignment', 'exam', 'experiment'] = 'manual'
    source_id: int | None = Field(default=None, gt=0)
    selection: Literal['highest', 'latest'] = 'highest'

    @field_validator('title')
    @classmethod
    def title_nonblank(cls, value):
        if not value.strip(): raise ValueError('名称不能为空')
        return value.strip()

    @model_validator(mode='after')
    def source_contract(self):
        if (self.source_kind == 'manual') != (self.source_id is None):
            raise ValueError('自动成绩必须绑定来源，人工成绩不能绑定来源')
        return self


class CategoryConfig(StrictModel):
    key: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    title: str = Field(min_length=1, max_length=120)
    weight: Decimal = Field(ge=0, le=100, decimal_places=4)
    columns: list[ColumnConfig] = Field(min_length=1, max_length=30)

    @field_validator('title')
    @classmethod
    def title_nonblank(cls, value):
        if not value.strip(): raise ValueError('名称不能为空')
        return value.strip()

    @model_validator(mode='after')
    def weights(self):
        if sum(c.weight for c in self.columns) != Decimal(100):
            raise ValueError('每个一级栏目内的二级栏目比例必须合计100%')
        return self


class ConfigUpdate(StrictModel):
    expected_revision: int = Field(ge=0)
    categories: list[CategoryConfig] = Field(min_length=1, max_length=15)

    @model_validator(mode='after')
    def unique_definitions(self):
        if sum(c.weight for c in self.categories) != Decimal(100):
            raise ValueError('一级栏目比例必须合计100%')
        category_keys = [c.key for c in self.categories]
        keys = [col.key for c in self.categories for col in c.columns]
        sources = [(col.source_kind, col.source_id) for c in self.categories for col in c.columns if col.source_id]
        if len(category_keys) != len(set(category_keys)) or len(keys) != len(set(keys)):
            raise ValueError('栏目标识不能重复')
        if len(sources) != len(set(sources)):
            raise ValueError('同一成绩来源不能重复计分')
        return self


class EntryUpdate(StrictModel):
    student_id: int = Field(gt=0)
    column_key: str = Field(min_length=1, max_length=64)
    state: Literal['scored', 'pending', 'missing', 'exempt', 'auto']
    score: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode='after')
    def state_values(self):
        if (self.state == 'scored') != (self.score is not None):
            raise ValueError('已评分需要0至100分；其他状态不能附带分数')
        if self.state in ('missing', 'exempt') and not (self.reason and self.reason.strip()):
            raise ValueError('确认缺交或免修必须填写原因')
        return self


class EntriesUpdate(StrictModel):
    expected_revision: int = Field(ge=1)
    entries: list[EntryUpdate] = Field(min_length=1, max_length=5000)

    @model_validator(mode='after')
    def unique_entries(self):
        keys = [(e.student_id, e.column_key) for e in self.entries]
        if len(keys) != len(set(keys)): raise ValueError('同一学生栏目不能重复修改')
        return self


class RevisionAction(StrictModel):
    expected_revision: int = Field(ge=1)
    expected_preview_hash: str | None = Field(default=None, pattern=r'^[a-f0-9]{64}$')
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('reason')
    @classmethod
    def reason_nonblank(cls, value):
        if not value.strip(): raise ValueError('必须填写原因')
        return value.strip()


class AdjustmentUpdate(RevisionAction):
    student_id: int = Field(gt=0)
    score: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)

"""HTTP 请求 / 响应模型（Pydantic v2）。

两种写入接口都支持"单条对象"或 ``{"items": [...]}`` 批量提交：
批量提交时只要有一条非法，整批拒绝、不写任何数据。
额外字段（如客户端试图回填的 published_at）一律拒绝。
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReleaseItem(StrictModel):
    number: str = Field(..., description="规程编号，全局唯一")
    body: str = Field(..., description="规程正文")
    valid_start: str = Field(..., description="UTC 适用区间起点（含），ISO-8601 带时区")
    valid_end: str = Field(..., description="UTC 适用区间终点（不含），ISO-8601 带时区")
    prerequisites: list[str] | None = Field(
        default=None,
        description="前置规程编号列表（可晚于本规程发布；不得含空白或重复编号）；"
                    "省略视为空列表")


class ReleaseBatch(StrictModel):
    items: list[ReleaseItem]


class CorrectionItem(StrictModel):
    number: str = Field(..., description="规程编号")
    base_revision: int = Field(..., ge=0, description="被替代版本号（首版为 0）")
    valid_start: str = Field(..., description="子区间起点（含），ISO-8601 带时区")
    valid_end: str = Field(..., description="子区间终点（不含），ISO-8601 带时区")
    body: str = Field(..., description="新正文（仅覆盖该子区间）")
    prerequisites: list[str] | None = Field(
        default=None,
        description="前置规程编号列表（不得含空白或重复编号）；"
                    "省略继承被替代版本的列表，显式空列表表示清空")


class CorrectionBatch(StrictModel):
    items: list[CorrectionItem]


class WithdrawalItem(StrictModel):
    number: str = Field(..., description="规程编号")
    revision: int = Field(..., ge=0, description="待撤回的更正版本号（首版 0 不可撤回）")


class WithdrawalBatch(StrictModel):
    items: list[WithdrawalItem]


class PackageRequest(StrictModel):
    """固定作业规程包：作业时刻 + 知悉时刻 + 一组规程编号（非空、不得重复）。"""
    work_time: str = Field(..., description="作业时刻，ISO-8601 带时区")
    known_as_of: str = Field(..., description="知悉时刻（不得晚于服务端当前时刻），ISO-8601 带时区")
    numbers: list[str] = Field(..., min_length=1,
                               description="规程编号列表，至少 1 个且不得重复")

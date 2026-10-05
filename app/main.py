"""FastAPI 入口：HTTP API 与统一错误处理。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import service
from .config import settings
from .db import init_db
from .errors import DomainError
from .schemas import (CorrectionBatch, CorrectionItem, PackageRequest,
                      ReleaseBatch, ReleaseItem, WithdrawalBatch,
                      WithdrawalItem)

app = FastAPI(
    title="安全作业规程历史回溯服务",
    version="1.0.0",
    description=("双时间维（适用时间 / 发布时间）规程版本库：首次发布、更正、"
                 "误发布更正的撤回（只追加事件）、按作业时刻+知悉时刻回溯查询、"
                 "两知悉时刻区间对比，以及作业规程包的固定、取回与复核。"),
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.exception_handler(DomainError)
async def _domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status,
                        content={"error": {"code": exc.code,
                                           "message": exc.message,
                                           "details": exc.details}})


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request,
                              exc: RequestValidationError) -> JSONResponse:
    # 包含额外字段（如试图回填的 published_at）、类型错误等
    return JSONResponse(status_code=422,
                        content={"error": {"code": "validation_error",
                                           "message": "请求参数校验失败",
                                           "details": {"errors": exc.errors()}}})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def index() -> dict:
    return {"service": "sop-history",
            "docs": "/docs",
            "health": "/health",
            "endpoints": [
                "POST /api/v1/packages",
                "GET  /api/v1/packages/{package_id}",
                "GET  /api/v1/packages/{package_id}/review",
                "POST /api/v1/releases",
                "POST /api/v1/corrections",
                "POST /api/v1/withdrawals",
                "GET  /api/v1/effective",
                "GET  /api/v1/procedures/{number}/history",
                "GET  /api/v1/procedures/{number}/diff",
            ]}


def _coerce_items(payload: dict, single_cls, batch_cls):
    """允许单条对象直接提交，或 {"items": [...]} 批量提交。"""
    try:
        if isinstance(payload, dict) and "items" in payload:
            return batch_cls(**payload).items
        return [single_cls(**payload)]
    except ValidationError as exc:
        # 手动校验与 FastAPI 入参校验走同一套错误响应
        raise RequestValidationError(exc.errors())


@app.post("/api/v1/releases", status_code=201)
async def create_release(request: Request) -> dict:
    """首次发布规程：编号、正文、UTC 适用区间 [起, 止)。"""
    payload = await request.json()
    items = _coerce_items(payload, ReleaseItem, ReleaseBatch)
    return service.publish_releases(items)


@app.post("/api/v1/corrections", status_code=201)
async def create_correction(request: Request) -> dict:
    """提交更正：编号、被替代版本、该版本当前生效区间内的子区间、新正文。

    服务端记录发布时间；批量提交时任一条目非法则整批拒绝。
    """
    payload = await request.json()
    items = _coerce_items(payload, CorrectionItem, CorrectionBatch)
    return service.submit_corrections(items)


@app.post("/api/v1/withdrawals", status_code=201)
async def create_withdrawal(request: Request) -> dict:
    """撤回误发布的更正：编号 + 更正版本号。

    撤回时刻由服务端记录（不接受回填）；只追加撤回事件、不删除原版本。
    首版、已撤回版本、已有子更正的版本一律拒绝；批量提交时任一条非法整批回滚。
    """
    payload = await request.json()
    items = _coerce_items(payload, WithdrawalItem, WithdrawalBatch)
    return service.withdraw_corrections(items)


@app.get("/api/v1/effective")
def effective(work_time: str, known_as_of: str | None = None) -> dict:
    """回溯查询：截至 known_as_of 已发布、且在 work_time 有效的正文与版本。"""
    return service.query_effective(work_time, known_as_of)


@app.get("/api/v1/procedures/{number}/history")
def procedure_history(number: str) -> dict:
    """审计视图：某规程的全部版本（含尚未对旧知悉时刻可见的版本）。"""
    return service.list_history(number)


@app.get("/api/v1/procedures/{number}/diff")
def procedure_diff(number: str, known_before: str, known_after: str,
                   work_start: str, work_end: str) -> dict:
    """两知悉时刻对比：同一规程在作业区间 [work_start, work_end) 内，
    先后两个知悉时刻可见的实际适用正文发生变化的最大片段。

    时间均须为 ISO-8601 带时区；known_before 必须严格早于 known_after；
    作业区间非空。编号不存在返回 404，其余非法入参返回 422；均不写历史。
    """
    return service.compare_interval(number, known_before, known_after,
                                    work_start, work_end)


@app.post("/api/v1/packages", status_code=201)
async def create_package(req: PackageRequest) -> dict:
    """固定作业规程包：作业时刻 + 知悉时刻 + 一组规程编号。

    编号重复、缺失，或任一规程在该作业时刻/知悉时刻下无适用正文，均整体
    拒绝、不留包；知悉时刻不得晚于服务端当前时刻。成功返回包标识、各规程
    当时适用的版本与正文及两种时刻；包一经形成即不可变。
    """
    return service.create_package(req.work_time, req.known_as_of, req.numbers)


@app.get("/api/v1/packages/{package_id}")
def get_package(package_id: str) -> dict:
    """按标识取回作业规程包：返回固定时原样快照（含正文），之后的更正、
    撤回均不改变该内容。包不存在返回 404 package_not_found。"""
    return service.get_package(package_id)


@app.get("/api/v1/packages/{package_id}/review")
def review_package(package_id: str, known_as_of: str) -> dict:
    """复核作业规程包：以不早于包内知悉时刻、不晚于服务端当前时刻的复核
    知悉时刻，按原作业时刻逐项返回包内版本（pinned）、彼时有效版本
    （current）及 changed，并给出整体 all_match。

    包不存在 -> 404；复核时间非法（含早于包内知悉时刻、晚于当前时刻、
    缺时区）-> 422。
    """
    return service.review_package(package_id, known_as_of)

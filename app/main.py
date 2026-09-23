"""FastAPI 入口：提交解析、按指纹查询、健康检查。

启动（lifespan）时自动建库建表完成初始化。
环境变量：
  DATABASE_PATH       SQLite 文件路径（默认 /data/resolutions.db）
  SOLVER_MAX_ATTEMPTS 回溯最大尝试次数（默认 100000）
"""

from __future__ import annotations

import contextlib
import os
from typing import Optional

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .models import ResolveRequest, ResolveResponse, fingerprint_from_request
from .semverx import SemverError
from .service import ResolveService
from .solver import SolverLimitError
from .storage import Store

DATABASE_PATH = os.getenv("DATABASE_PATH", "/data/resolutions.db")
MAX_ATTEMPTS = int(os.getenv("SOLVER_MAX_ATTEMPTS", "100000"))

_store: Optional[Store] = None
_service: Optional[ResolveService] = None


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    # 启动时完成数据库初始化（目录 + 表结构）
    global _store, _service
    _store = Store(DATABASE_PATH)
    _service = ResolveService(_store, max_attempts=MAX_ATTEMPTS)
    yield


app = FastAPI(
    title="依赖版本兼容解析 API",
    description=(
        "提交组件清单（名称、候选版本及 semver 依赖范围），"
        "服务构建有向依赖图并用确定性回溯选择满足全部约束的版本；"
        "无解时返回最小冲突链与约束来源。请求指纹（SHA-256）支持幂等复用。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(SemverError)
async def _semver_error_handler(_: Request, exc: SemverError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": f"semver 解析失败: {exc}"})


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    return {"status": "ok", "stored_resolutions": _store.count() if _store else 0}


@app.post(
    "/api/v1/resolve",
    response_model=ResolveResponse,
    tags=["resolve"],
    summary="提交（或复用）一次依赖解析",
)
async def resolve(req: ResolveRequest) -> JSONResponse:
    """相同请求体（与字段顺序无关）具有相同指纹，重复提交直接复用已存结果。

    新求解返回 ``201``；幂等命中返回 ``200`` 且 ``reused=true``。
    """
    fp = fingerprint_from_request(req)
    try:
        # CPU 密集型求解放到线程池，避免阻塞事件循环
        result = await anyio.to_thread.run_sync(_service.resolve, fp, req)
    except SolverLimitError as exc:
        raise HTTPException(status_code=509, detail=str(exc))
    return JSONResponse(status_code=200 if result.get("reused") else 201, content=result)


@app.get(
    "/api/v1/resolutions/{fingerprint}",
    response_model=ResolveResponse,
    tags=["resolve"],
    summary="按请求指纹查询解析结果",
)
def get_resolution(fingerprint: str) -> dict:
    result = _store.get(fingerprint)  # type: ignore[union-attr]
    if result is None:
        raise HTTPException(status_code=404, detail="未找到该指纹对应的解析结果")
    return result

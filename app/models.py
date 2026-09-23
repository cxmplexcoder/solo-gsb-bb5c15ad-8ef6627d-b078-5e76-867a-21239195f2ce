"""API 请求/响应模型与请求指纹。"""

from __future__ import annotations

import hashlib
import json
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .semverx import RangeSpec, Version


def _validate_version(value: str) -> str:
    Version.parse(value)
    return value


def _validate_spec(value: Optional[str]) -> Optional[str]:
    if value is None:
        return value
    RangeSpec.parse(value)
    return value


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class DependencyIn(BaseModel):
    name: str = Field(..., min_length=1, description="被依赖组件名")
    spec: Optional[str] = Field("*", description="semver 范围，如 ^1.2.0、>=1.0.0 <2.0.0")

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("组件名不能为空")
        return v

    @field_validator("spec")
    @classmethod
    def _spec(cls, v):
        return _validate_spec(v)


class CandidateIn(BaseModel):
    version: str = Field(..., description="候选版本（semver，如 1.2.3）")
    dependencies: List[DependencyIn] = Field(default_factory=list)

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        return _validate_version(v)


class ComponentIn(BaseModel):
    name: str = Field(..., min_length=1)
    candidates: List[CandidateIn] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("组件名不能为空")
        return v


class ResolveRequest(BaseModel):
    components: List[ComponentIn] = Field(..., description="组件清单")
    requirements: List[DependencyIn] = Field(
        ..., min_length=1, description="顶层（根）依赖要求"
    )

    @field_validator("components")
    @classmethod
    def _unique_components(cls, v):
        names = [c.name for c in v]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"组件名重复: {', '.join(dup)}")
        for comp in v:
            vers = [c.version for c in comp.candidates]
            dupv = sorted({x for x in vers if vers.count(x) > 1})
            if dupv:
                raise ValueError(
                    f"组件 {comp.name!r} 的候选版本重复: {', '.join(dupv)}"
                )
        return v


# ---------------------------------------------------------------------------
# 响应模型（显式声明，便于 OpenAPI 文档）
# ---------------------------------------------------------------------------


class AssignmentOut(BaseModel):
    name: str
    version: str


class EdgeOut(BaseModel):
    source: str
    source_version: str
    target: str
    target_version: str
    spec: str


class GraphOut(BaseModel):
    nodes: List[str]
    edges: List[EdgeOut]


class ConstraintOut(BaseModel):
    spec: str
    requester: str
    requester_version: Optional[str]
    chain: List[str]


class ConflictOut(BaseModel):
    component: Optional[str]
    component_known: bool = True
    available_versions: List[str] = Field(default_factory=list)
    constraints: List[ConstraintOut] = Field(default_factory=list)
    chains: List[List[str]] = Field(default_factory=list)
    message: str


class ResolveResponse(BaseModel):
    fingerprint: str
    status: str = Field(description="sat=找到解；unsat=约束冲突无解")
    reused: bool = Field(description="是否复用了此前缓存的结果")
    created_at: str
    solve_ms: float
    attempts: int
    assignments: List[AssignmentOut] = Field(default_factory=list)
    resolution_order: List[str] = Field(default_factory=list)
    graph: Optional[GraphOut] = None
    conflict: Optional[ConflictOut] = None


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------


def canonical_payload(payload: dict) -> dict:
    """把原始 payload 规整为确定形式。

    除 JSON 键顺序无关（序列化时 sort_keys）外，清单中的集合元素
    （组件、候选版本、依赖、根要求）的排列顺序也不影响语义，统一排序，
    使“同一语义请求”无论书写顺序如何都得到相同指纹。
    """
    req = ResolveRequest.model_validate(payload)
    data = req.model_dump()

    def ver_key(v: str):
        return Version.parse(v)._key()

    for comp in data["components"]:
        comp["candidates"].sort(key=lambda c: ver_key(c["version"]))
        for cand in comp["candidates"]:
            cand["dependencies"].sort(key=lambda d: (d["name"], d["spec"] or ""))
    data["components"].sort(key=lambda c: c["name"])
    data["requirements"].sort(key=lambda d: (d["name"], d["spec"] or ""))
    return data


def fingerprint_from_request(req: ResolveRequest) -> str:
    """对规范化后的请求取 SHA-256（字段/集合顺序无关）。"""
    canonical = canonical_payload(req.model_dump())
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprint(payload: dict) -> str:
    """对规范化后的请求体取 SHA-256；同一语义请求（字段/列表顺序无关）指纹相同。"""
    canonical = canonical_payload(payload)
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

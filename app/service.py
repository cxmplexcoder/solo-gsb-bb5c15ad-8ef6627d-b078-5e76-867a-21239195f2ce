"""解析服务：请求模型 -> 求解器目录 -> 求解 -> 结果组装/持久化。"""

from __future__ import annotations

import threading
import time
from typing import Dict

from .models import ResolveRequest
from .semverx import RangeSpec, Version
from .solver import (
    Candidate,
    CatalogItem,
    DepSpec,
    Solver,
)
from .storage import Store, utc_now_iso


def build_catalog(req: ResolveRequest) -> Dict[str, CatalogItem]:
    catalog: Dict[str, CatalogItem] = {}
    for comp in req.components:
        candidates = []
        for cand in comp.candidates:
            deps = tuple(
                DepSpec(name=d.name, spec=RangeSpec.parse(d.spec if d.spec is not None else "*"))
                for d in cand.dependencies
            )
            candidates.append(
                Candidate(
                    version=Version.parse(cand.version),
                    raw_version=cand.version,
                    dependencies=deps,
                )
            )
        catalog[comp.name] = CatalogItem(name=comp.name, candidates=candidates)
    return catalog


def run_resolution(req: ResolveRequest, max_attempts: int) -> dict:
    catalog = build_catalog(req)
    roots = [(d.name, RangeSpec.parse(d.spec if d.spec is not None else "*")) for d in req.requirements]
    solver = Solver(catalog, roots, max_attempts=max_attempts)
    return solver.solve()


class ResolveService:
    """组合求解器与存储；按指纹加锁，保证并发相同请求只求解一次。"""

    def __init__(self, store: Store, max_attempts: int = 100_000):
        self.store = store
        self.max_attempts = max_attempts
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, fp: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(fp)
            if lock is None:
                lock = threading.Lock()
                self._locks[fp] = lock
            return lock

    def resolve(self, fp: str, req: ResolveRequest) -> dict:
        cached = self.store.get(fp)
        if cached is not None:
            return cached

        lock = self._lock_for(fp)
        with lock:
            # 双检：可能在等锁期间已被其他请求算完
            cached = self.store.get(fp)
            if cached is not None:
                return cached

            started = time.perf_counter()
            outcome = run_resolution(req, self.max_attempts)
            solve_ms = round((time.perf_counter() - started) * 1000, 3)
            created_at = utc_now_iso()

            # 入库内容只保存求解产物（查询时补齐指纹/时间等元数据）
            storable = {
                k: v
                for k, v in outcome.items()
                if k in ("assignments", "resolution_order", "graph", "conflict")
            }
            self.store.save(
                fp,
                status=outcome["status"],
                result=storable,
                solve_ms=solve_ms,
                attempts=outcome["attempts"],
                created_at=created_at,
            )
            result = dict(storable)
            result.update(
                fingerprint=fp,
                status=outcome["status"],
                reused=False,
                created_at=created_at,
                solve_ms=solve_ms,
                attempts=outcome["attempts"],
            )
            return result

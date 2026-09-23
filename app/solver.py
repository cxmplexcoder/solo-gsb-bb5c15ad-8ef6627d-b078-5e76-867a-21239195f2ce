"""确定性回溯依赖版本求解器。

输入组件目录（每个组件带候选版本列表，每个版本带 semver 依赖范围），
构造有向依赖图，并按 MRV（最少剩余候选）+ 版本降序的确定性顺序回溯，
选出同时满足全部约束的一组版本。

无解时收集各分支的“域清空冲突”，取最短链并做包含极小化（MUS 近似），
返回最小冲突链与每条约束的来源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .semverx import RangeSpec, Version

ROOT = "root"


# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DepSpec:
    name: str
    spec: RangeSpec


@dataclass(frozen=True)
class Candidate:
    version: Version
    raw_version: str
    dependencies: Tuple[DepSpec, ...]


@dataclass
class CatalogItem:
    name: str
    candidates: List[Candidate]


@dataclass(frozen=True)
class Origin:
    """一条约束的来源。

    requester          施加约束的组件名（根需求为 "root"）
    requester_version  施加约束时该组件选中的版本（根需求为 None）
    path               从根到施加者的标签链，如 ("root", "web@1.0.0")
    """

    requester: str
    requester_version: Optional[str]
    path: Tuple[str, ...]


@dataclass(frozen=True)
class Constraint:
    spec: RangeSpec
    origin: Origin

    @property
    def key(self) -> Tuple:
        return (self.origin.path, self.spec.raw)


@dataclass
class Frame:
    """一次赋值的搜索栈帧。"""

    component: str
    candidates: List[Candidate]
    index: int = 0
    path: Tuple[str, ...] = ()
    reason: Optional[Constraint] = None          # 选中该组件所满足的来源约束
    added: List[Tuple[str, Constraint]] = field(default_factory=list)


class SolverLimitError(RuntimeError):
    """超过最大尝试次数（保护性中止，结果不确定）。"""


# ---------------------------------------------------------------------------
# 求解器
# ---------------------------------------------------------------------------


class Solver:
    def __init__(
        self,
        catalog: Dict[str, CatalogItem],
        roots: List[Tuple[str, RangeSpec]],
        max_attempts: int = 100_000,
    ):
        self.catalog = catalog
        self.roots = roots
        self.max_attempts = max_attempts

        self.constraints: Dict[str, List[Constraint]] = {}
        self.assigned: Dict[str, Candidate] = {}
        self.frames: List[Frame] = []
        self.attempts = 0

        # 每个候选版本预排序：semver 降序，同版本按原始字符串稳定去重
        self.domains: Dict[str, List[Candidate]] = {}
        for name, item in catalog.items():
            seen: set = set()
            uniq: List[Candidate] = []
            for cand in item.candidates:
                if cand.version in seen:
                    continue
                seen.add(cand.version)
                uniq.append(cand)
            uniq.sort(key=lambda c: c.version, reverse=True)
            self.domains[name] = uniq

    # ---------------- 基础工具 ----------------

    def _active_components(self) -> List[str]:
        names = {name for name, _ in self.roots}
        names.update(self.constraints.keys())
        return sorted(names)

    def _feasible(self, component: str) -> List[Candidate]:
        """返回该组件当前满足全部活动约束的候选（降序）。"""
        cands = self.domains.get(component, [])
        specs = self.constraints.get(component, [])
        out = []
        for cand in cands:
            if all(c.spec.satisfies(cand.version) for c in specs):
                out.append(cand)
        return out

    @staticmethod
    def _origin_label(origin: Origin) -> str:
        return origin.path[-1]

    def _pick_reason(self, component: str) -> Optional[Constraint]:
        """选择触发该组件赋值的来源约束（来源路径字典序最小，根最先）。"""
        cons = self.constraints.get(component, [])
        if not cons:
            return None
        return sorted(cons, key=lambda c: c.origin.path)[0]

    # ---------------- 赋值 / 回滚 ----------------

    def _assign(self, frame: Frame, cand: Candidate) -> List[Tuple[str, Constraint]]:
        self.assigned[frame.component] = cand
        label = f"{frame.component}@{cand.raw_version}"
        frame.path = (frame.reason.origin.path if frame.reason else (ROOT,)) + (label,)

        added: List[Tuple[str, Constraint]] = []
        for dep in sorted(cand.dependencies, key=lambda d: d.name):
            origin = Origin(
                requester=frame.component,
                requester_version=cand.raw_version,
                path=frame.path,
            )
            cons = Constraint(spec=dep.spec, origin=origin)
            self.constraints.setdefault(dep.name, []).append(cons)
            added.append((dep.name, cons))
        frame.added = added
        return added

    def _rollback_to(self, target_frame: Frame) -> None:
        """弹出 target_frame 之后的所有帧，并撤销 target_frame 当前赋值。"""
        # 先撤掉 target_frame 自己的赋值（保留帧本身用于重试）
        self._undo_frame(target_frame)
        while self.frames and self.frames[-1] is not target_frame:
            self._undo_frame(self.frames[-1])
            self.frames.pop()

    def _undo_frame(self, frame: Frame) -> None:
        """撤销一个帧的赋值与其引入的约束。"""
        for comp, cons in frame.added:
            bucket = self.constraints.get(comp)
            if bucket and cons in bucket:
                bucket.remove(cons)
                if not bucket:
                    del self.constraints[comp]
        self.assigned.pop(frame.component, None)
        frame.added = []

    # ---------------- 冲突记录 ----------------

    def _snapshot(self, component: str) -> dict:
        records = sorted(
            self.constraints.get(component, []),
            key=lambda c: (c.origin.path, c.spec.raw),
        )
        return {
            "component": component,
            "records": records,
            "depth": max((len(c.origin.path) for c in records), default=1),
        }

    @staticmethod
    def _conflict_key(snap: dict) -> Tuple:
        return (
            len(snap["records"]),
            snap["depth"],
            snap["component"],
            tuple(c.spec.raw for c in snap["records"]),
        )

    def _minimize(self, snap: dict) -> List[Constraint]:
        """对冲突约束集合做包含极小化：尽量删掉仍然无解的约束。"""
        component = snap["component"]
        kept = list(snap["records"])  # 已按稳定顺序排序
        cands = self.domains.get(component, [])

        def infeasible(subset: List[Constraint]) -> bool:
            # 空约束集永远可行（缺组件时，零条要求不算冲突）
            if not subset:
                return False
            return not any(
                all(cons.spec.satisfies(cand.version) for cons in subset)
                for cand in cands
            )

        # 当前集合本身必然为空交集；逐个尝试删除
        minimal: List[Constraint] = []
        for cons in kept:
            trial = minimal + [c for c in kept if c is not cons and c not in minimal]
            if infeasible(trial):
                kept = trial  # 可删除
            else:
                minimal.append(cons)  # 必须保留
        return minimal

    # ---------------- 主求解循环 ----------------

    def solve(self) -> dict:
        # 根约束
        for name, spec in sorted(self.roots, key=lambda r: r[0]):
            self.constraints.setdefault(name, []).append(
                Constraint(spec=spec, origin=Origin(ROOT, None, (ROOT,)))
            )

        best_conflict: Optional[dict] = None

        while True:
            # 1) 已赋值组件可能因后续回溯重赋值而失效 => 立即冲突
            broken: Optional[str] = None
            for name in sorted(self.assigned):
                if not self._feasible(name):
                    broken = name
                    break
            if broken is not None:
                snap = self._snapshot(broken)
                if best_conflict is None or self._conflict_key(snap) < self._conflict_key(
                    best_conflict
                ):
                    best_conflict = snap
                if not self._backtrack():
                    return self._unsat_result(best_conflict)
                continue

            # 2) 找出尚未赋值的活动组件中候选最少者（MRV）
            target: Optional[str] = None
            target_domain: List[Candidate] = []
            for name in self._active_components():
                if name in self.assigned:
                    continue
                domain = self._feasible(name)
                key = (len(domain), name)
                if target is None or key < (len(target_domain), target):
                    target, target_domain = name, domain

            # 3) 全部赋值完成 => SAT
            if target is None:
                return self._sat_result()

            # 4) 域清空 => 记录冲突并回溯（快照必须在回滚前取）
            if not target_domain:
                snap = self._snapshot(target)
                if best_conflict is None or self._conflict_key(snap) < self._conflict_key(
                    best_conflict
                ):
                    best_conflict = snap
                if not self._backtrack():
                    return self._unsat_result(best_conflict)
                continue

            # 5) 正常赋值
            self.attempts += 1
            if self.attempts > self.max_attempts:
                raise SolverLimitError(
                    f"求解尝试次数超过上限 {self.max_attempts}，请扩大 SOLVER_MAX_ATTEMPTS"
                )
            frame = Frame(
                component=target,
                candidates=target_domain,
                reason=self._pick_reason(target),
            )
            self.frames.append(frame)
            self._assign(frame, target_domain[0])

    def _backtrack(self) -> bool:
        """回滚到下一个还有候选可试的栈帧；全部试完返回 False。"""
        while self.frames:
            frame = self.frames[-1]
            if frame.index + 1 < len(frame.candidates):
                frame.index += 1
                self.attempts += 1
                if self.attempts > self.max_attempts:
                    raise SolverLimitError(
                        f"求解尝试次数超过上限 {self.max_attempts}，请扩大 SOLVER_MAX_ATTEMPTS"
                    )
                # 撤销该帧当前赋值与其后帧（帧本身保留以便重试）
                self._rollback_to(frame)
                self._assign(frame, frame.candidates[frame.index])
                return True
            # 该帧候选已试尽：彻底弹出，继续找上一帧
            self._undo_frame(frame)
            self.frames.pop()
        return False

    # ---------------- 结果构造 ----------------

    def _sat_result(self) -> dict:
        assignments = []
        order = [f.component for f in self.frames]
        for name in order:
            cand = self.assigned[name]
            assignments.append(
                {"name": name, "version": cand.raw_version}
            )

        edges = []
        for f in self.frames:
            cand = self.assigned[f.component]
            for dep in sorted(cand.dependencies, key=lambda d: d.name):
                edges.append(
                    {
                        "source": f.component,
                        "source_version": cand.raw_version,
                        "target": dep.name,
                        "target_version": self.assigned[dep.name].raw_version,
                        "spec": dep.spec.raw,
                    }
                )

        return {
            "status": "sat",
            "assignments": assignments,
            "resolution_order": order,
            "graph": {"nodes": order, "edges": edges},
            "attempts": self.attempts,
        }

    def _unsat_result(self, snap: dict) -> dict:
        if snap is None:  # 理论上不会发生（根为空时直接 SAT）
            return {
                "status": "unsat",
                "conflict": {
                    "component": None,
                    "available_versions": [],
                    "constraints": [],
                    "chains": [],
                    "message": "无解",
                },
                "attempts": self.attempts,
            }

        minimal = self._minimize(snap)
        component = snap["component"]
        known = component in self.domains

        constraints_out = []
        chains = []
        for cons in minimal:
            o = cons.origin
            chain = list(o.path) + [component]
            chains.append(chain)
            constraints_out.append(
                {
                    "spec": cons.spec.raw,
                    "requester": o.requester,
                    "requester_version": o.requester_version,
                    "chain": chain,
                }
            )

        if not known:
            message = (
                f"组件 {component!r} 在清单中不存在，但存在对它的依赖要求"
            )
        else:
            joined = " 且 ".join(c.spec.raw for c in minimal)
            message = (
                f"组件 {component!r} 没有任何候选版本能同时满足: {joined}"
            )

        return {
            "status": "unsat",
            "conflict": {
                "component": component,
                "component_known": known,
                "available_versions": [c.raw_version for c in self.domains.get(component, [])],
                "constraints": constraints_out,
                "chains": chains,
                "message": message,
            },
            "attempts": self.attempts,
        }

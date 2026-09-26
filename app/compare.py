"""两份已保存解析结果的比较。

输入两份 ``status=sat`` 的求解产物（``assignments`` + ``graph.edges``），
产出：

* 按组件名对齐的赋值变化：``added`` / ``removed`` / ``upgraded`` /
  ``downgraded`` / ``other``（仅 build metadata 等造成的非升非降变化）；
* 依赖边的增删（边以 ``(source, target, spec)`` 标识，与版本无关）；
* 受影响的上游组件：以全部发生变化的组件为起点，沿两份图的反向边
  （取并集）做多源 BFS，为每个可达的未变化组件返回一条**最短**来源链；
  链等长时按节点字典序取最小者，保证结果稳定。

纯函数实现，不依赖存储与 HTTP，便于直接单测。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .semverx import Version

# 边的身份：依赖由谁以什么范围施加给谁；两端具体选定的版本不算边的身份
EdgeKey = Tuple[str, str, str]


class CompareError(Exception):
    """比较过程的逻辑错误（基类）。"""


class ResolutionNotFound(CompareError):
    """指纹没有对应的已保存结果。"""

    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint
        super().__init__(f"未找到指纹 {fingerprint} 对应的解析结果")


class ResolutionUnsat(CompareError):
    """某一侧结果状态为 unsat，无法比较。"""

    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint
        super().__init__(f"指纹 {fingerprint} 的解析结果状态为 unsat，不可比较")


# ---------------------------------------------------------------------------
# 取数小工具
# ---------------------------------------------------------------------------


def _assignments(result: dict) -> Dict[str, str]:
    return {a["name"]: a["version"] for a in result.get("assignments", [])}


def _edges(result: dict) -> List[dict]:
    graph = result.get("graph") or {}
    return list(graph.get("edges") or [])


def _edge_key(edge: dict) -> EdgeKey:
    return edge["source"], edge["target"], edge["spec"]


def _edge_sort_key(edge: dict) -> Tuple[str, str, str]:
    return edge["source"], edge["target"], edge["spec"]


# ---------------------------------------------------------------------------
# 赋值对齐
# ---------------------------------------------------------------------------


def _component_changes(
    base_ver: Dict[str, str], target_ver: Dict[str, str]
) -> Dict[str, List[dict]]:
    """按组件名对齐两份赋值，归入五类变化。"""
    buckets: Dict[str, List[dict]] = {
        "added": [],
        "removed": [],
        "upgraded": [],
        "downgraded": [],
        "other": [],
    }

    for name in sorted(set(base_ver) | set(target_ver)):
        old = base_ver.get(name)
        new = target_ver.get(name)
        if old is None:
            buckets["added"].append({"name": name, "version": new})
        elif new is None:
            buckets["removed"].append({"name": name, "version": old})
        elif old == new:
            continue
        else:
            old_parsed = Version.parse(old)
            new_parsed = Version.parse(new)
            entry = {"name": name, "version_from": old, "version_to": new}
            if new_parsed > old_parsed:
                buckets["upgraded"].append(entry)
            elif new_parsed < old_parsed:
                buckets["downgraded"].append(entry)
            else:
                # semver 核心与预发布相同但原始写法不同（如 build metadata）
                buckets["other"].append(entry)

    # 各桶内按组件名稳定排序（遍历已排序，这里显式固定契约）
    for items in buckets.values():
        items.sort(key=lambda e: e["name"])
    return buckets


# ---------------------------------------------------------------------------
# 依赖边增删
# ---------------------------------------------------------------------------


def _edge_changes(
    base_edges: List[dict], target_edges: List[dict]
) -> Tuple[List[dict], List[dict]]:
    base_map = {_edge_key(e): e for e in base_edges}
    target_map = {_edge_key(e): e for e in target_edges}

    added = [e for key, e in target_map.items() if key not in base_map]
    removed = [e for key, e in base_map.items() if key not in target_map]
    added.sort(key=_edge_sort_key)
    removed.sort(key=_edge_sort_key)
    return added, removed


# ---------------------------------------------------------------------------
# 上游影响（沿两份图反向边的多源最短路径）
# ---------------------------------------------------------------------------


def _reverse_edges(base_edges: List[dict], target_edges: List[dict]) -> Dict[str, Set[str]]:
    """合并两份图的反向邻接表：node -> 直接依赖它的组件集合。"""
    reverse: Dict[str, Set[str]] = {}
    for edge in (*base_edges, *target_edges):
        reverse.setdefault(edge["target"], set()).add(edge["source"])
    return reverse


def _impacted(
    changed: Set[str], reverse: Dict[str, Set[str]]
) -> List[dict]:
    """从变化组件出发做反向多源 BFS。

    每个可达组件记录一条“组件 -> ... -> 变化源头”的最短来源链
    （即从受影响组件出发，沿反向边到达任一变化组件的最短路径）；
    距离相同时按链的字典序取最小，结果整体按组件名稳定排序。
    """
    best: Dict[str, Tuple[str, ...]] = {}
    frontier = sorted(changed)
    for seed in frontier:
        best[seed] = (seed,)

    while frontier:
        # 候选路径统一排序：等价于按 (上一跳, 路径) 字典序扩展，
        # 因此同距离下首个到达的路径就是字典序最小的最短链
        candidates: List[Tuple[str, Tuple[str, ...]]] = []
        for node in frontier:
            for upstream in reverse.get(node, ()):  # set 迭代顺序由后续排序兜底
                if upstream not in best:
                    candidates.append((upstream, best[node] + (upstream,)))
        candidates.sort(key=lambda item: item[1])
        next_frontier: List[str] = []
        seen: Set[str] = set()
        for upstream, path in candidates:
            if upstream in best:
                continue
            best[upstream] = path
            if upstream not in seen:
                seen.add(upstream)
                next_frontier.append(upstream)
        frontier = next_frontier

    return [
        {"component": name, "source_chain": list(best[name])}
        for name in sorted(best)
        if name not in changed
    ]


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def compare_results(base: dict, target: dict) -> dict:
    """比较两份 sat 结果。调用方需自行保证两侧存在且状态为 sat。"""
    base_ver = _assignments(base)
    target_ver = _assignments(target)

    components = _component_changes(base_ver, target_ver)
    added_edges, removed_edges = _edge_changes(_edges(base), _edges(target))

    changed_names = {
        e["name"]
        for bucket in ("added", "removed", "upgraded", "downgraded", "other")
        for e in components[bucket]
    }
    impacted = _impacted(changed_names, _reverse_edges(_edges(base), _edges(target)))

    return {
        "components": components,
        "edges": {"added": added_edges, "removed": removed_edges},
        "impacted": impacted,
    }


def load_pair(
    store: Any, base_fp: str, target_fp: str
) -> Tuple[Optional[dict], Optional[dict]]:
    """从存储取出两份结果；先校验存在性（404 语义），再校验状态（409 语义）。"""
    base = store.get(base_fp)
    if base is None:
        raise ResolutionNotFound(base_fp)
    target = store.get(target_fp)
    if target is None:
        raise ResolutionNotFound(target_fp)
    if base.get("status") == "unsat":
        raise ResolutionUnsat(base_fp)
    if target.get("status") == "unsat":
        raise ResolutionUnsat(target_fp)
    return base, target

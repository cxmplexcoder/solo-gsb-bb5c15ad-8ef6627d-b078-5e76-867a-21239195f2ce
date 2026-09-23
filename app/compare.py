"""两份已保存解析结果的对比。

对比内容：

1. **组件赋值变化**：按组件名对齐两份 ``sat`` 结果的赋值，分为
   ``added``（新增）、``removed``（移除）、``upgraded``（升级）、
   ``downgraded``（降级）与 ``changed``（其他版本变化，如 build metadata
   不同但 semver 核心相同）。
2. **依赖边增删**：以
   ``(source, source_version, target, target_version, spec)`` 完整五元组
   作为边标识，求两份图的集合差。
3. **受影响上游**：把五类变化组件都作为源头，在每份图内沿该图自身的反向
   边做多源 BFS（每条来源链都完整落在某一份图内）；新图 BFS 描述新状态下
   的影响面，旧图 BFS 额外覆盖“经已移除组件才能追溯到”的上游。合并两份图
   的候选链，为每个受影响组件保留一条最短链，链等长时取节点标签字典序
   最小者，保证输出稳定。
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List, Set, Tuple

from .semverx import SemverError, Version

# 组件变化分类
ADDED = "added"
REMOVED = "removed"
UPGRADED = "upgraded"
DOWNGRADED = "downgraded"
CHANGED = "changed"  # semver 不可比（如仅 build metadata 不同）的版本变化

EdgeTuple = Tuple[str, str, str, str, str]


def _assignments(result: dict) -> Dict[str, str]:
    return {a["name"]: a["version"] for a in result.get("assignments", [])}


def _edges(result: dict) -> Set[EdgeTuple]:
    graph = result.get("graph") or {}
    return {
        (
            e["source"],
            e["source_version"],
            e["target"],
            e["target_version"],
            e["spec"],
        )
        for e in graph.get("edges", [])
    }


def _classify_version(base_version: str, other_version: str) -> str:
    try:
        old = Version.parse(base_version)
        new = Version.parse(other_version)
    except SemverError:
        # 入库版本已校验过；防御性回退：非同串归为“其他版本变化”
        return CHANGED
    if new > old:
        return UPGRADED
    if new < old:
        return DOWNGRADED
    # 核心与预发布相同但原始串不同（如 build metadata 差异）
    return CHANGED


def diff_components(base: dict, other: dict) -> Dict[str, List[dict]]:
    """按组件名对齐两份赋值并分类。各类内按组件名稳定排序。"""
    old = _assignments(base)
    new = _assignments(other)

    buckets: Dict[str, List[dict]] = {
        ADDED: [],
        REMOVED: [],
        UPGRADED: [],
        DOWNGRADED: [],
        CHANGED: [],
    }
    for name in sorted(set(old) | set(new)):
        in_old, in_new = name in old, name in new
        if in_new and not in_old:
            buckets[ADDED].append({"name": name, "version": new[name]})
        elif in_old and not in_new:
            buckets[REMOVED].append({"name": name, "version": old[name]})
        else:
            old_v, new_v = old[name], new[name]
            if old_v == new_v:
                continue
            kind = _classify_version(old_v, new_v)
            buckets[kind].append(
                {"name": name, "base_version": old_v, "other_version": new_v}
            )
    return buckets


def _edge_out(edge: EdgeTuple) -> dict:
    source, source_version, target, target_version, spec = edge
    return {
        "source": source,
        "source_version": source_version,
        "target": target,
        "target_version": target_version,
        "spec": spec,
    }


def diff_edges(base: dict, other: dict) -> Dict[str, List[dict]]:
    """比较两份图的依赖边集合，返回新增/移除的边。"""
    old_edges = _edges(base)
    new_edges = _edges(other)
    return {
        "added": [_edge_out(e) for e in sorted(new_edges - old_edges)],
        "removed": [_edge_out(e) for e in sorted(old_edges - new_edges)],
    }


def _graph_indexes(
    edge_set: Set[EdgeTuple], versions: Dict[str, str]
) -> Tuple[Dict[str, List[EdgeTuple]], Dict[str, List[EdgeTuple]]]:
    """构建单份图的反向/正向邻接（只保留与当前选中版本一致的边）。"""
    reverse: Dict[str, List[EdgeTuple]] = defaultdict(list)
    outgoing: Dict[str, List[EdgeTuple]] = defaultdict(list)
    for edge in edge_set:
        if versions.get(edge[0]) != edge[1] or versions.get(edge[2]) != edge[3]:
            continue
        reverse[edge[2]].append(edge)
        outgoing[edge[0]].append(edge)
    for bucket in reverse.values():
        bucket.sort()
    for bucket in outgoing.values():
        bucket.sort()
    return dict(reverse), dict(outgoing)


def _trace_one_graph(
    edge_set: Set[EdgeTuple],
    versions: Dict[str, str],
    seed_names: Set[str],
    display_heads: Dict[str, str],
) -> Dict[str, Tuple[str, ...]]:
    """在单份图内做多源 BFS，返回每个可达节点的一条最短来源链。

    ``seed_names``：本图中作为源头的组件名；``display_heads`` 给出各源头
    在输出链中的链头标签（通常取新版本）。扩展只沿“属于该组件当前选中
    版本”的反向边，因此链中除链头外的每个 ``name@version`` 都存在于本图。

    两阶段：先在组件名图上做标准多源 BFS 得到最短距离，再按距离递增 DP
    选出字典序最小的最短链。
    """
    reverse, outgoing = _graph_indexes(edge_set, versions)

    # 1) 标准多源 BFS：源头距离 0；沿反向边首次到达即记录最短距离。
    dist: Dict[str, int] = {name: 0 for name in seed_names}
    queue: deque = deque(sorted(seed_names))
    while queue:
        current = queue.popleft()
        for edge in reverse.get(current, []):
            source = edge[0]
            if source not in dist:
                dist[source] = dist[current] + 1
                queue.append(source)

    # 2) 按距离递增重建链：深度 d 的上游节点 node，沿“node -> 深度 d-1 的
    #    依赖项”的边接续前驱链，取字典序最小者。源头节点固定使用展示标签。
    paths: Dict[str, Tuple[str, ...]] = {
        name: (display_heads.get(name, f"{name}@{versions[name]}"),)
        for name in seed_names
    }
    for depth in range(1, max(dist.values(), default=0) + 1):
        for node in sorted(n for n, d in dist.items() if d == depth):
            node_label = f"{node}@{versions[node]}"
            best: Tuple[str, ...] | None = None
            for edge in outgoing.get(node, []):
                pred = edge[2]  # node 依赖的下游组件
                if dist.get(pred) != depth - 1 or pred not in paths:
                    continue
                candidate = paths[pred] + (node_label,)
                if best is None or candidate < best:
                    best = candidate
            if best is not None:
                paths[node] = best
    return paths


def affected_upstreams(changed: Set[str], base: dict, other: dict) -> List[dict]:
    """计算直接或间接受变化影响的上游组件（含一条最短来源链）。

    每份图各自做一次 BFS：

    - **新图（other）**：从所有在新图中存在的变化组件出发，链头取新版本，
      描述“新状态下谁直接/间接依赖了变化点”；
    - **旧图（base）**：沿旧图反向边扩展，覆盖“变化点在旧图中曾有的、现在
      已消失的上游”（如经被移除组件才能到达的祖父节点）；链头仍展示变化
      点的新版本，移除组件无新版本时退回旧版本。

    合并两份图的候选链，取链长短、等长时标签字典序最小者；变化组件自身
    （``changed``）不作为受影响项输出。
    """
    old_versions = _assignments(base)
    new_versions = _assignments(other)
    old_edges = _edges(base)
    new_edges = _edges(other)

    candidates: Dict[str, List[Tuple[str, ...]]] = defaultdict(list)
    for edge_set, versions in ((old_edges, old_versions), (new_edges, new_versions)):
        seed_names = {name for name in changed if name in versions}
        # 链头统一展示变化点的新版本；移除组件无新版本时退回本图（旧）版本
        display_heads = {}
        for name in seed_names:
            if name in new_versions:
                display_heads[name] = f"{name}@{new_versions[name]}"
            else:
                display_heads[name] = f"{name}@{old_versions[name]}"
        paths = _trace_one_graph(edge_set, versions, seed_names, display_heads)
        for name, chain in paths.items():
            if len(chain) >= 2:  # 排除源头自身
                candidates[name].append(chain)

    out = []
    for name in sorted(candidates):
        if name in changed:
            continue
        chain = min(candidates[name], key=lambda c: (len(c), c))
        out.append(
            {
                "component": name,
                "chain": list(chain),
                "distance": len(chain) - 1,
            }
        )
    out.sort(key=lambda item: (item["distance"], item["chain"], item["component"]))
    return out


def compare_resolutions(base: dict, other: dict) -> dict:
    """组装两份 sat 解析结果的完整对比报告。"""
    component_diff = diff_components(base, other)
    changed_names = {
        item["name"] for bucket in component_diff.values() for item in bucket
    }
    edge_diff = diff_edges(base, other)
    affected = affected_upstreams(changed_names, base, other)

    return {
        "base": {
            "fingerprint": base["fingerprint"],
            "status": base["status"],
            "created_at": base["created_at"],
        },
        "other": {
            "fingerprint": other["fingerprint"],
            "status": other["status"],
            "created_at": other["created_at"],
        },
        "components": component_diff,
        "edges": edge_diff,
        "affected": affected,
    }

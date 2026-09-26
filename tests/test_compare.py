"""比较模块单元测试：赋值分类、依赖边增删、反向边最短链与稳定性。"""

from app.compare import compare_results


def _edge(source, target, spec="*", sv="1.0.0", tv="1.0.0"):
    return {
        "source": source,
        "source_version": sv,
        "target": target,
        "target_version": tv,
        "spec": spec,
    }


def _result(versions, edges=None, order=None):
    names = order or [n for n, _ in versions]
    return {
        "assignments": [{"name": n, "version": v} for n, v in versions],
        "graph": {"nodes": names, "edges": edges or []},
    }


def _names(items, key="name"):
    return [e[key] for e in items]


class TestComponentClassification:
    def test_added_removed_upgraded_downgraded(self):
        base = _result(
            [("app", "1.0.0"), ("gone", "3.0.0"), ("hold", "1.0.0"), ("down", "2.1.0")]
        )
        target = _result(
            [("app", "2.0.0"), ("fresh", "1.0.0"), ("hold", "1.0.0"), ("down", "2.0.0")]
        )
        diff = compare_results(base, target)["components"]

        assert diff["added"] == [{"name": "fresh", "version": "1.0.0"}]
        assert diff["removed"] == [{"name": "gone", "version": "3.0.0"}]
        assert diff["upgraded"] == [
            {"name": "app", "version_from": "1.0.0", "version_to": "2.0.0"}
        ]
        assert diff["downgraded"] == [
            {"name": "down", "version_from": "2.1.0", "version_to": "2.0.0"}
        ]
        assert diff["other"] == []

    def test_prerelease_order_counts_as_upgrade(self):
        base = _result([("lib", "1.0.0-rc.1")])
        target = _result([("lib", "1.0.0")])
        diff = compare_results(base, target)["components"]
        assert _names(diff["upgraded"]) == ["lib"]
        assert diff["downgraded"] == [] and diff["other"] == []

    def test_other_when_only_build_metadata_differs(self):
        # build metadata 不参与 semver 比较 => 非升非降，归入 other
        base = _result([("lib", "1.0.0")])
        target = _result([("lib", "1.0.0+exp.sha.5114f85")])
        diff = compare_results(base, target)["components"]
        assert diff["other"] == [
            {"name": "lib", "version_from": "1.0.0", "version_to": "1.0.0+exp.sha.5114f85"}
        ]
        assert diff["upgraded"] == [] and diff["downgraded"] == []

    def test_buckets_sorted_by_name(self):
        base = _result([("z", "1.0.0"), ("a", "1.0.0"), ("m", "9.0.0")])
        target = _result(
            [("z", "2.0.0"), ("a", "2.0.0"), ("m", "1.0.0"), ("b", "1.0.0")]
        )
        comps = compare_results(base, target)["components"]
        assert _names(comps["upgraded"]) == ["a", "z"]
        assert _names(comps["downgraded"]) == ["m"]
        assert _names(comps["added"]) == ["b"]


class TestEdgeChanges:
    def test_edges_added_and_removed(self):
        base = _result(
            [("web", "1.0.0"), ("lib", "1.0.0"), ("util", "1.0.0")],
            [
                _edge("web", "lib", "^1.0.0"),
                _edge("web", "util", "~1.0"),
            ],
        )
        target = _result(
            [("web", "2.0.0"), ("lib", "2.0.0"), ("newdep", "1.0.0")],
            [
                # spec 变化：旧边删除、新边增加（即使两端组件相同）
                _edge("web", "lib", "^2.0.0", sv="2.0.0", tv="2.0.0"),
                _edge("web", "newdep", ">=1.0.0", sv="2.0.0"),
            ],
        )
        edges = compare_results(base, target)["edges"]

        assert [(e["source"], e["target"], e["spec"]) for e in edges["added"]] == [
            ("web", "lib", "^2.0.0"),
            ("web", "newdep", ">=1.0.0"),
        ]
        assert [(e["source"], e["target"], e["spec"]) for e in edges["removed"]] == [
            ("web", "lib", "^1.0.0"),
            ("web", "util", "~1.0"),
        ]
        # 新边携带目标图的版本信息
        assert edges["added"][0]["source_version"] == "2.0.0"

    def test_same_edge_key_unchanged_despite_version_bump(self):
        # spec 不变、两端组件不变：即使版本升级也不算边增删
        base = _result(
            [("web", "1.0.0"), ("lib", "1.0.0")], [_edge("web", "lib", "^1.0.0")]
        )
        target = _result(
            [("web", "2.0.0"), ("lib", "1.5.0")],
            [_edge("web", "lib", "^1.0.0", sv="2.0.0", tv="1.5.0")],
        )
        edges = compare_results(base, target)["edges"]
        assert edges["added"] == [] and edges["removed"] == []


class TestImpacted:
    def test_direct_and_indirect_upstream_with_shortest_chain(self):
        base = _result(
            [("suite", "1.0.0"), ("platform", "1.0.0"), ("web", "1.0.0"),
             ("lib", "1.0.0"), ("util", "1.0.0")],
            [
                _edge("suite", "platform"),
                _edge("platform", "web"),
                _edge("web", "lib"),
                _edge("web", "util"),
            ],
        )
        target = _result(
            [("suite", "1.0.0"), ("platform", "1.0.0"), ("web", "2.0.0"),
             ("lib", "2.0.0")],
            [
                _edge("suite", "platform"),
                _edge("platform", "web"),
                _edge("web", "lib", tv="2.0.0"),
            ],
        )
        impacted = {e["component"]: e["source_chain"]
                    for e in compare_results(base, target)["impacted"]}
        # 变化种子 web/lib/util 不出现在 impacted 中
        assert set(impacted) == {"platform", "suite"}
        assert impacted["platform"] == ["web", "platform"]
        # 间接上游链到最近的变化种子
        assert impacted["suite"] == ["web", "platform", "suite"]

    def test_diamond_chain_prefers_lexicographic_on_tie(self):
        # top 可经 left 或 right 到达变化点 leaf（等长），取字典序最小
        base = _result(
            [("top", "1.0.0"), ("left", "1.0.0"), ("right", "1.0.0"),
             ("leaf", "1.0.0")],
            [_edge("top", "left"), _edge("top", "right"),
             _edge("left", "leaf"), _edge("right", "leaf")],
        )
        target = _result(
            [("top", "1.0.0"), ("left", "1.0.0"), ("right", "1.0.0"),
             ("leaf", "2.0.0")],
            [_edge("top", "left"), _edge("top", "right"),
             _edge("left", "leaf", tv="2.0.0"), _edge("right", "leaf", tv="2.0.0")],
        )
        impacted = {e["component"]: e["source_chain"]
                    for e in compare_results(base, target)["impacted"]}
        assert impacted["left"] == ["leaf", "left"]
        assert impacted["right"] == ["leaf", "right"]
        assert impacted["top"] == ["leaf", "left", "top"]

    def test_reverse_edges_from_both_graphs(self):
        # 同一组件在两份图中都未变化，但 newparent -> kept 这条边只存在于目标图
        # （反向边按题意取两份图并集，故仍能沿目标图反向边追到 newparent）
        base = _result(
            [("newparent", "1.0.0"), ("kept", "1.0.0"), ("leaf", "1.0.0")],
            [_edge("kept", "leaf")],
        )
        target = _result(
            [("newparent", "1.0.0"), ("kept", "1.0.0"), ("leaf", "2.0.0")],
            [_edge("newparent", "kept"), _edge("kept", "leaf", tv="2.0.0")],
        )
        impacted = {e["component"]: e["source_chain"]
                    for e in compare_results(base, target)["impacted"]}
        assert impacted["kept"] == ["leaf", "kept"]
        assert impacted["newparent"] == ["leaf", "kept", "newparent"]

    def test_removed_component_seeds_reverse_scan_in_base_graph(self):
        # middle 在目标图被移除（种子）；top 在两侧都未变化，仅经基准图反向边
        # middle <- top 可达
        base = _result(
            [("top", "1.0.0"), ("middle", "1.0.0"), ("leaf", "1.0.0")],
            [_edge("top", "middle"), _edge("middle", "leaf")],
        )
        target = _result([("top", "1.0.0"), ("leaf", "2.0.0")], [])
        impacted = {e["component"]: e["source_chain"]
                    for e in compare_results(base, target)["impacted"]}
        assert impacted["top"] == ["middle", "top"]
        assert "middle" not in impacted  # 移除组件本身是种子

    def test_identical_results_have_no_diff(self):
        result = _result(
            [("a", "1.0.0"), ("b", "2.0.0")], [_edge("a", "b", "^2.0.0")]
        )
        diff = compare_results(result, dict(result))
        comps = diff["components"]
        assert all(comps[k] == [] for k in
                   ("added", "removed", "upgraded", "downgraded", "other"))
        assert diff["edges"]["added"] == [] and diff["edges"]["removed"] == []
        assert diff["impacted"] == []

    def test_output_is_deterministic(self):
        base = _result(
            [("z", "1.0.0"), ("a", "1.0.0"), ("m", "1.0.0")],
            [_edge("a", "m"), _edge("z", "m")],
        )
        target = _result(
            [("z", "2.0.0"), ("a", "1.0.0"), ("m", "2.0.0"), ("q", "1.0.0")],
            [_edge("a", "m", tv="2.0.0"), _edge("z", "m", sv="2.0.0", tv="2.0.0")],
        )
        first = compare_results(base, target)
        second = compare_results(base, target)
        assert first == second
        impacted_order = [e["component"] for e in first["impacted"]]
        assert impacted_order == sorted(impacted_order)

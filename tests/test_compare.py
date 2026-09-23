"""对比逻辑单元测试：组件分类、边增删、反向边影响面与最短来源链。"""

from app.compare import compare_resolutions, diff_components, diff_edges


def result(fp, assignments, edges=(), status="sat"):
    """构造与存储层读出形态一致的解析结果字典。"""
    return {
        "fingerprint": fp,
        "status": status,
        "reused": True,
        "created_at": f"time-{fp}",
        "solve_ms": 1.0,
        "attempts": 1,
        "assignments": [
            {"name": n, "version": v} for n, v in assignments
        ],
        "resolution_order": [n for n, _ in assignments],
        "graph": {"nodes": [n for n, _ in assignments], "edges": list(edges)},
        "conflict": None,
    }


def edge(s, sv, t, tv, spec="*"):
    return {
        "source": s,
        "source_version": sv,
        "target": t,
        "target_version": tv,
        "spec": spec,
    }


def names(items):
    return [i["name"] for i in items]


class TestComponentDiff:
    def test_all_five_categories(self):
        base = result(
            "a",
            [
                ("up", "1.0.0"),
                ("down", "2.1.0"),
                ("same", "1.4.0"),
                ("gone", "1.0.0"),
            ],
        )
        other = result(
            "b",
            [
                ("up", "1.2.0"),
                ("down", "2.0.0"),
                ("same", "1.4.0"),
                ("fresh", "3.0.0"),
            ],
        )
        diff = diff_components(base, other)
        assert names(diff["added"]) == ["fresh"]
        assert diff["added"][0]["version"] == "3.0.0"
        assert names(diff["removed"]) == ["gone"]
        assert diff["removed"][0]["version"] == "1.0.0"

        (up,) = diff["upgraded"]
        assert up == {"name": "up", "base_version": "1.0.0", "other_version": "1.2.0"}
        (down,) = diff["downgraded"]
        assert down["name"] == "down"
        assert down["base_version"] == "2.1.0"
        assert down["other_version"] == "2.0.0"
        assert diff["changed"] == []

    def test_prerelease_upgrade_downgrade(self):
        base = result("a", [("c", "1.0.0-alpha.1")])
        other = result("b", [("c", "1.0.0")])
        assert names(diff_components(base, other)["upgraded"]) == ["c"]
        diff = diff_components(other, base)
        assert names(diff["downgraded"]) == ["c"]

    def test_equal_raw_versions_ignored(self):
        base = result("a", [("c", "1.0.0")])
        other = result("b", [("c", "1.0.0")])
        diff = diff_components(base, other)
        assert all(not bucket for bucket in diff.values())

    def test_build_metadata_only_diff_is_changed(self):
        # semver 核心与预发布相同、仅 build metadata 不同 => changed
        base = result("a", [("c", "1.0.0+alpha")])
        other = result("b", [("c", "1.0.0+beta")])
        (item,) = diff_components(base, other)["changed"]
        assert item == {
            "name": "c",
            "base_version": "1.0.0+alpha",
            "other_version": "1.0.0+beta",
        }
        assert diff_components(base, other)["upgraded"] == []

    def test_buckets_sorted_by_name(self):
        base = result("a", [("z", "1.0.0"), ("a", "2.0.0")])
        other = result("b", [("z", "2.0.0"), ("a", "1.0.0")])
        diff = diff_components(base, other)
        assert names(diff["upgraded"]) == ["z"]
        assert names(diff["downgraded"]) == ["a"]


class TestEdgeDiff:
    def test_added_and_removed_full_tuples(self):
        base = result(
            "a",
            [("app", "1.0.0"), ("lib", "1.0.0")],
            [edge("app", "1.0.0", "lib", "1.0.0", "^1.0.0")],
        )
        other = result(
            "b",
            [("app", "1.0.0"), ("lib", "2.0.0")],
            [
                edge("app", "1.0.0", "lib", "2.0.0", "^2.0.0"),
                edge("app", "1.0.0", "extra", "1.0.0"),
            ],
        )
        diff = diff_edges(base, other)
        assert len(diff["removed"]) == 1
        assert diff["removed"][0]["target_version"] == "1.0.0"
        assert diff["removed"][0]["spec"] == "^1.0.0"
        assert {e["target_version"] for e in diff["added"]} == {"2.0.0", "1.0.0"}
        # 新增边按五元组稳定排序
        key = lambda e: (
            e["source"], e["source_version"], e["target"],
            e["target_version"], e["spec"],
        )
        assert diff["added"] == sorted(diff["added"], key=key)

    def test_identical_graph_no_edge_changes(self):
        edges = [edge("app", "1.0.0", "lib", "1.0.0")]
        diff = diff_edges(result("a", [("app", "1.0.0"), ("lib", "1.0.0")], edges),
                          result("b", [("app", "1.0.0"), ("lib", "1.0.0")], edges))
        assert diff["added"] == []
        assert diff["removed"] == []

    def test_spec_only_change_counts_as_remove_and_add(self):
        base = result(
            "a", [("app", "1.0.0"), ("lib", "1.0.0")],
            [edge("app", "1.0.0", "lib", "1.0.0", "^1.0.0")],
        )
        other = result(
            "b", [("app", "1.0.0"), ("lib", "1.0.0")],
            [edge("app", "1.0.0", "lib", "1.0.0", "~1.0.0")],
        )
        diff = diff_edges(base, other)
        assert diff["removed"][0]["spec"] == "^1.0.0"
        assert diff["added"][0]["spec"] == "~1.0.0"


class TestAffected:
    def test_direct_and_indirect_upstreams_with_shortest_chain(self):
        # changed: lib@1 -> lib@2；app 直接依赖 lib，platform 经 app 间接依赖
        base = result(
            "a",
            [("platform", "1.0.0"), ("app", "1.0.0"), ("lib", "1.0.0")],
            [
                edge("platform", "1.0.0", "app", "1.0.0"),
                edge("app", "1.0.0", "lib", "1.0.0"),
            ],
        )
        other = result(
            "b",
            [("platform", "1.0.0"), ("app", "1.0.0"), ("lib", "2.0.0")],
            [
                edge("platform", "1.0.0", "app", "1.0.0"),
                edge("app", "1.0.0", "lib", "2.0.0"),
            ],
        )
        report = compare_resolutions(base, other)
        affected = {a["component"]: a for a in report["affected"]}
        assert affected["app"]["chain"] == ["lib@2.0.0", "app@1.0.0"]
        assert affected["app"]["distance"] == 1
        assert affected["platform"]["chain"] == [
            "lib@2.0.0", "app@1.0.0", "platform@1.0.0"
        ]
        assert affected["platform"]["distance"] == 2
        # 变化组件自身不出现在 affected 中
        assert "lib" not in affected
        # 排序：距离升序
        assert [a["component"] for a in report["affected"]] == ["app", "platform"]

    def test_each_chain_lives_in_one_graph(self):
        # lib 升级。watch_old 的依赖边在 other 中被移除，watch_new 的依赖边
        # 在 other 中新出现（两个 watcher 组件本身都未变化）；两条直接影响
        # 链分别来自旧图/新图 BFS，链头都是新版本 lib@2.0.0。
        base = result(
            "a",
            [("watch_old", "1.0.0"), ("watch_new", "1.0.0"), ("lib", "1.0.0")],
            [edge("watch_old", "1.0.0", "lib", "1.0.0")],
        )
        other = result(
            "b",
            [("watch_old", "1.0.0"), ("watch_new", "1.0.0"), ("lib", "2.0.0")],
            [edge("watch_new", "1.0.0", "lib", "2.0.0")],
        )
        report = compare_resolutions(base, other)
        affected = {a["component"]: a["chain"] for a in report["affected"]}
        assert affected == {
            "watch_old": ["lib@2.0.0", "watch_old@1.0.0"],
            "watch_new": ["lib@2.0.0", "watch_new@1.0.0"],
        }

    def test_indirect_via_removed_component_in_base_graph(self):
        # 旧图：grand -> mid(将被移除) -> lib(将升级)；新图保留 grand 与 lib，
        # 仅移除 mid（grand 在新图中成为无依赖边的孤立节点）。
        # grand 未变化：它直接受 mid 移除影响（距离 1，旧图 BFS 给出）。
        base = result(
            "a",
            [("grand", "1.0.0"), ("mid", "1.0.0"), ("lib", "1.0.0")],
            [
                edge("grand", "1.0.0", "mid", "1.0.0"),
                edge("mid", "1.0.0", "lib", "1.0.0"),
            ],
        )
        other = result("b", [("grand", "1.0.0"), ("lib", "2.0.0")], [])
        report = compare_resolutions(base, other)
        affected = {a["component"]: a for a in report["affected"]}
        assert "mid" not in affected  # mid 自身被移除，在 components 段报告
        assert affected["grand"]["chain"] == ["mid@1.0.0", "grand@1.0.0"]
        assert affected["grand"]["distance"] == 1
        assert [a["component"] for a in report["affected"]] == ["grand"]

    def test_indirect_chain_through_unchanged_intermediate(self):
        # 中间层 mid 未变化时，lib 升级应沿 mid 追溯到 grand（距离 2），
        # 且新、旧两份图都能给出该链。
        base = result(
            "a",
            [("grand", "1.0.0"), ("mid", "1.0.0"), ("lib", "1.0.0")],
            [
                edge("grand", "1.0.0", "mid", "1.0.0"),
                edge("mid", "1.0.0", "lib", "1.0.0"),
            ],
        )
        other = result(
            "b",
            [("grand", "1.0.0"), ("mid", "1.0.0"), ("lib", "2.0.0")],
            [
                edge("grand", "1.0.0", "mid", "1.0.0"),
                edge("mid", "1.0.0", "lib", "2.0.0"),
            ],
        )
        report = compare_resolutions(base, other)
        affected = {a["component"]: a for a in report["affected"]}
        assert affected["mid"]["chain"] == ["lib@2.0.0", "mid@1.0.0"]
        assert affected["grand"]["chain"] == [
            "lib@2.0.0", "mid@1.0.0", "grand@1.0.0"
        ]
        assert affected["grand"]["distance"] == 2

    def test_shortest_of_multiple_paths_and_tie_break(self):
        # z 有两条等长路径到 lib：lib->a->z 与 lib->b->z，取标签字典序最小
        base = other = result(
            "a",
            [("lib", "1.0.0"), ("a", "1.0.0"), ("b", "1.0.0"), ("z", "1.0.0")],
            [
                edge("a", "1.0.0", "lib", "1.0.0"),
                edge("b", "1.0.0", "lib", "1.0.0"),
                edge("z", "1.0.0", "a", "1.0.0"),
                edge("z", "1.0.0", "b", "1.0.0"),
            ],
        )
        # 用升级场景制造一个 seed
        other = result(
            "b",
            [("lib", "2.0.0"), ("a", "1.0.0"), ("b", "1.0.0"), ("z", "1.0.0")],
            [
                edge("a", "1.0.0", "lib", "2.0.0"),
                edge("b", "1.0.0", "lib", "2.0.0"),
                edge("z", "1.0.0", "a", "1.0.0"),
                edge("z", "1.0.0", "b", "1.0.0"),
            ],
        )
        report = compare_resolutions(base, other)
        z = next(a for a in report["affected"] if a["component"] == "z")
        assert z["chain"] == ["lib@2.0.0", "a@1.0.0", "z@1.0.0"]
        assert z["distance"] == 2

    def test_cycles_are_safe_and_stable(self):
        # 3 节点环 a -> b -> c -> a；仅 c 升级，a、b 不变。
        # 从 c 沿反向边：b 直接依赖 c（距离 1），a 经 b 距离 2。
        # 环不会使 BFS 死循环。
        base_edges = [
            edge("a", "1.0.0", "b", "1.0.0"),
            edge("b", "1.0.0", "c", "1.0.0"),
            edge("c", "1.0.0", "a", "1.0.0"),
        ]
        base = result(
            "a", [("a", "1.0.0"), ("b", "1.0.0"), ("c", "1.0.0")], base_edges
        )
        other_edges = [
            edge("a", "1.0.0", "b", "1.0.0"),
            edge("b", "1.0.0", "c", "2.0.0"),
            edge("c", "2.0.0", "a", "1.0.0"),
        ]
        other = result(
            "b", [("a", "1.0.0"), ("b", "1.0.0"), ("c", "2.0.0")], other_edges
        )
        report = compare_resolutions(base, other)
        affected = {x["component"]: x for x in report["affected"]}
        assert set(affected) == {"a", "b"}
        assert affected["b"]["distance"] == 1
        assert affected["b"]["chain"] == ["c@2.0.0", "b@1.0.0"]
        assert affected["a"]["distance"] == 2
        assert affected["a"]["chain"] == ["c@2.0.0", "b@1.0.0", "a@1.0.0"]
        # 重复调用结果稳定
        assert compare_resolutions(base, other) == report

    def test_added_component_seed_chain_uses_other_version(self):
        base = result("a", [("app", "1.0.0")], [])
        other = result(
            "b",
            [("app", "1.0.0"), ("lib", "1.0.0")],
            [edge("app", "1.0.0", "lib", "1.0.0")],
        )
        report = compare_resolutions(base, other)
        (app,) = report["affected"]
        assert app["chain"] == ["lib@1.0.0", "app@1.0.0"]

    def test_report_carries_result_refs(self):
        base = result("aaaa", [("c", "1.0.0")])
        other = result("bbbb", [("c", "2.0.0")])
        report = compare_resolutions(base, other)
        assert report["base"]["fingerprint"] == "aaaa"
        assert report["other"]["fingerprint"] == "bbbb"
        assert report["base"]["status"] == "sat"

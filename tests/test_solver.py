import pytest

from app.models import ResolveRequest
from app.service import run_resolution
from app.solver import SolverLimitError


def build(components, requirements):
    return ResolveRequest.model_validate(
        {"components": components, "requirements": requirements}
    )


def comp(name, versions):
    """versions: ["1.0.0"] 或 [("1.0.0", {"lib": "^1"})]"""
    cands = []
    for item in versions:
        if isinstance(item, tuple):
            ver, deps = item
            cands.append(
                {"version": ver,
                 "dependencies": [{"name": k, "spec": v} for k, v in sorted(deps.items())]}
            )
        else:
            cands.append({"version": item, "dependencies": []})
    return {"name": name, "candidates": cands}


def assignment(result):
    return {a["name"]: a["version"] for a in result["assignments"]}


class TestSat:
    def test_simple_highest_version(self):
        req = build(
            [comp("web", ["1.0.0", "2.0.0", "2.1.0"])],
            [{"name": "web", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "sat"
        assert assignment(res) == {"web": "2.1.0"}

    def test_range_picks_highest_satisfying(self):
        req = build(
            [comp("web", ["1.0.0", "2.0.0", "2.1.0"])],
            [{"name": "web", "spec": "~2.0"}],
        )
        res = run_resolution(req, 100000)
        assert assignment(res) == {"web": "2.0.0"}

    def test_backtracking_finds_lower_version(self):
        # app 2.0.0 依赖 lib ^2（不存在），必须回退到 1.0.0 -> lib 1
        req = build(
            [
                comp("app", [
                    ("1.0.0", {"lib": "^1.0.0"}),
                    ("2.0.0", {"lib": "^2.0.0"}),
                ]),
                comp("lib", ["1.0.0", "1.5.0"]),
            ],
            [{"name": "app", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "sat"
        assert assignment(res) == {"app": "1.0.0", "lib": "1.5.0"}
        assert res["attempts"] >= 2

    def test_diamond_dependency(self):
        # top -> a,b；a 要 lib ^1.2，b 要 lib ~1.2 => lib 1.2.x 最高
        req = build(
            [
                comp("top", [("1.0.0", {"a": "*", "b": "*"})]),
                comp("a", [("1.0.0", {"lib": "^1.2.0"})]),
                comp("b", [("1.0.0", {"lib": "~1.2.0"})]),
                comp("lib", ["1.1.0", "1.2.0", "1.2.9", "1.3.0"]),
            ],
            [{"name": "top", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "sat"
        assert assignment(res)["lib"] == "1.2.9"

    def test_graph_edges_and_order(self):
        req = build(
            [
                comp("app", [("1.0.0", {"lib": "^1"})]),
                comp("lib", ["1.0.0"]),
            ],
            [{"name": "app", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["resolution_order"] == ["app", "lib"]
        edges = {(e["source"], e["target"]): e for e in res["graph"]["edges"]}
        assert edges[("app", "lib")]["target_version"] == "1.0.0"

    def test_cycles_are_supported(self):
        # 组件图允许环；每个组件选一个版本即可
        req = build(
            [
                comp("a", [("1.0.0", {"b": "*"})]),
                comp("b", [("1.0.0", {"a": "*"})]),
            ],
            [{"name": "a", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "sat"
        assert assignment(res) == {"a": "1.0.0", "b": "1.0.0"}

    def test_deterministic_across_input_order(self):
        def make(lib_order):
            return build(
                [
                    comp("x", [("1.0.0", {"lib": "^1"})]),
                    comp("lib", lib_order),
                ],
                [{"name": "x", "spec": "*"}],
            )

        r1 = run_resolution(make(["1.0.0", "1.4.0", "1.2.0"]), 100000)
        r2 = run_resolution(make(["1.2.0", "1.0.0", "1.4.0"]), 100000)
        assert assignment(r1) == assignment(r2) == {"x": "1.0.0", "lib": "1.4.0"}

    def test_attempt_limit_raises(self):
        # 强制只有 1 次尝试额度：app 首次选择 2.0.0 触发冲突，重试即超限
        req = build(
            [
                comp("app", [
                    ("1.0.0", {"lib": "^1.0.0"}),
                    ("2.0.0", {"lib": "^2.0.0"}),
                ]),
                comp("lib", ["1.0.0"]),
            ],
            [{"name": "app", "spec": "*"}],
        )
        with pytest.raises(SolverLimitError):
            run_resolution(req, max_attempts=1)


class TestUnsat:
    def test_direct_range_conflict(self):
        req = build(
            [comp("lib", ["1.0.0", "2.0.0"])],
            [{"name": "lib", "spec": "^1.0.0"}, {"name": "lib", "spec": "^2.0.0"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "unsat"
        conflict = res["conflict"]
        assert conflict["component"] == "lib"
        specs = sorted(c["spec"] for c in conflict["constraints"])
        assert specs == ["^1.0.0", "^2.0.0"]
        # 根约束链
        assert conflict["chains"] == [["root", "lib"], ["root", "lib"]]

    def test_transitive_conflict_reports_chain(self):
        # top -> a (lib ^1), top -> b (lib ^2)
        req = build(
            [
                comp("top", [("1.0.0", {"a": "*", "b": "*"})]),
                comp("a", [("1.0.0", {"lib": "^1.0.0"})]),
                comp("b", [("1.0.0", {"lib": "^2.0.0"})]),
                comp("lib", ["1.0.0", "2.0.0"]),
            ],
            [{"name": "top", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "unsat"
        constraints = res["conflict"]["constraints"]
        requesters = sorted((c["requester"], c["spec"]) for c in constraints)
        assert requesters == [("a", "^1.0.0"), ("b", "^2.0.0")]
        chains = [tuple(c["chain"]) for c in constraints]
        assert ("root", "top@1.0.0", "a@1.0.0", "lib") in chains
        assert ("root", "top@1.0.0", "b@1.0.0", "lib") in chains

    def test_unknown_component(self):
        req = build(
            [comp("app", [("1.0.0", {"ghost": "*"})])],
            [{"name": "app", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "unsat"
        assert res["conflict"]["component"] == "ghost"
        assert res["conflict"]["component_known"] is False

    def test_minimal_subset_excludes_satisfied_constraint(self):
        # 三条约束：^1、^2、>=3；真正极小冲突只需 ^1 + ^2（或 ^1 + >=3）
        # 求包含极小集合，约束数应为 2
        req = build(
            [comp("lib", ["1.0.0", "2.0.0", "3.0.0"])],
            [
                {"name": "lib", "spec": "^1.0.0"},
                {"name": "lib", "spec": "^2.0.0"},
                {"name": "lib", "spec": ">=3.0.0"},
            ],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "unsat"
        assert len(res["conflict"]["constraints"]) == 2

    def test_unsat_through_backtracking_multiple_parents(self):
        # a1 要 lib<2，b1 要 lib>=2，且只有这一组版本 => 深度回溯后无解
        req = build(
            [
                comp("top", [("1.0.0", {"a": "*", "b": "*"})]),
                comp("a", [("1.0.0", {"lib": "<2.0.0"})]),
                comp("b", [("1.0.0", {"lib": ">=2.0.0"})]),
                comp("lib", ["1.0.0", "2.0.0"]),
            ],
            [{"name": "top", "spec": "*"}],
        )
        res = run_resolution(req, 100000)
        assert res["status"] == "unsat"
        assert res["conflict"]["component"] == "lib"

"""API 端到端测试：提交、幂等复用、指纹查询、错误处理。"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SOLVER_MAX_ATTEMPTS", "100000")
    # 延迟导入，确保环境变量生效
    import importlib

    import app.main as main_mod

    importlib.reload(main_mod)
    with TestClient(main_mod.app) as client:
        yield client


def _payload():
    return {
        "components": [
            {
                "name": "app",
                "candidates": [
                    {"version": "1.0.0", "dependencies": [{"name": "lib", "spec": "^1.0.0"}]},
                    {"version": "2.0.0", "dependencies": [{"name": "lib", "spec": "^2.0.0"}]},
                ],
            },
            {"name": "lib", "candidates": [{"version": "1.0.0"}, {"version": "1.5.0"}]},
        ],
        "requirements": [{"name": "app", "spec": "*"}],
    }


class TestResolve:
    def test_sat_returns_201(self, client):
        r = client.post("/api/v1/resolve", json=_payload())
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "sat"
        assert body["reused"] is False
        assert {a["name"]: a["version"] for a in body["assignments"]} == {
            "app": "1.0.0",
            "lib": "1.5.0",
        }
        assert len(body["fingerprint"]) == 64

    def test_idempotent_replay_returns_200(self, client):
        first = client.post("/api/v1/resolve", json=_payload())
        assert first.status_code == 201
        fp = first.json()["fingerprint"]

        # 字段顺序不同也应得到同一指纹（规范化）
        shuffled = {
            "requirements": _payload()["requirements"],
            "components": list(reversed(_payload()["components"])),
        }
        second = client.post("/api/v1/resolve", json=shuffled)
        assert second.status_code == 200
        body = second.json()
        assert body["reused"] is True
        assert body["fingerprint"] == fp

    def test_get_by_fingerprint(self, client):
        created = client.post("/api/v1/resolve", json=_payload()).json()
        fp = created["fingerprint"]

        r = client.get(f"/api/v1/resolutions/{fp}")
        assert r.status_code == 200
        assert r.json()["fingerprint"] == fp
        assert r.json()["status"] == "sat"

    def test_get_unknown_fingerprint_404(self, client):
        r = client.get("/api/v1/resolutions/" + "0" * 64)
        assert r.status_code == 404

    def test_unsat_conflict_payload(self, client):
        payload = {
            "components": [{"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "2.0.0"}]}
            ],
            "requirements": [
                {"name": "lib", "spec": "^1.0.0"},
                {"name": "lib", "spec": "^2.0.0"},
            ],
        }
        r = client.post("/api/v1/resolve", json=payload)
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "unsat"
        conflict = body["conflict"]
        assert conflict["component"] == "lib"
        assert sorted(c["spec"] for c in conflict["constraints"]) == ["^1.0.0", "^2.0.0"]
        assert "没有任何候选版本" in conflict["message"]
        # 冲突结果同样可幂等复用
        fp = body["fingerprint"]
        again = client.post("/api/v1/resolve", json=payload)
        assert again.status_code == 200
        assert again.json()["reused"] is True
        assert client.get(f"/api/v1/resolutions/{fp}").json()["status"] == "unsat"


class TestValidation:
    def test_bad_version_422(self, client):
        payload = _payload()
        payload["components"][0]["candidates"][0]["version"] = "not-semver"
        r = client.post("/api/v1/resolve", json=payload)
        assert r.status_code == 422

    def test_bad_spec_422(self, client):
        payload = _payload()
        payload["requirements"][0]["spec"] = ">>1.0.0"
        r = client.post("/api/v1/resolve", json=payload)
        assert r.status_code == 422

    def test_duplicate_candidate_422(self, client):
        payload = _payload()
        payload["components"][1]["candidates"].append({"version": "1.0.0"})
        r = client.post("/api/v1/resolve", json=payload)
        assert r.status_code == 422

    def test_duplicate_component_422(self, client):
        payload = _payload()
        payload["components"].append({"name": "lib", "candidates": []})
        r = client.post("/api/v1/resolve", json=payload)
        assert r.status_code == 422

    def test_empty_requirements_422(self, client):
        payload = _payload()
        payload["requirements"] = []
        r = client.post("/api/v1/resolve", json=payload)
        assert r.status_code == 422


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    client.post("/api/v1/resolve", json=_payload())
    r2 = client.get("/health")
    assert r2.json()["stored_resolutions"] == 1


# ---------------------------------------------------------------------------
# POST /api/v1/resolutions/compare
# ---------------------------------------------------------------------------


def _compare_base_payload():
    """基线：app 1.0.0 -> lib ^1，platform -> app；含一个将被移除的组件。"""
    return {
        "components": [
            {
                "name": "platform",
                "candidates": [
                    {"version": "1.0.0", "dependencies": [{"name": "app", "spec": "*"}]},
                ],
            },
            {
                "name": "app",
                "candidates": [
                    {"version": "1.0.0", "dependencies": [{"name": "lib", "spec": "^1.0.0"}]},
                ],
            },
            {"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "1.5.0"}, {"version": "2.0.0"}]},
            {"name": "legacy", "candidates": [{"version": "1.0.0"}]},
        ],
        "requirements": [
            {"name": "platform", "spec": "*"},
            {"name": "legacy", "spec": "*"},
        ],
    }


def _compare_other_payload():
    """对比：app 升级到 2.0.0 -> lib ^2；legacy 根要求消失。"""
    return {
        "components": [
            {
                "name": "platform",
                "candidates": [
                    {"version": "1.0.0", "dependencies": [{"name": "app", "spec": "*"}]},
                ],
            },
            {
                "name": "app",
                "candidates": [
                    {"version": "2.0.0", "dependencies": [{"name": "lib", "spec": "^2.0.0"}]},
                ],
            },
            {"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "1.5.0"}, {"version": "2.0.0"}]},
            {"name": "legacy", "candidates": [{"version": "1.0.0"}]},
        ],
        "requirements": [{"name": "platform", "spec": "*"}],
    }


class TestCompare:
    def _two_fingerprints(self, client):
        base = client.post("/api/v1/resolve", json=_compare_base_payload()).json()
        other = client.post("/api/v1/resolve", json=_compare_other_payload()).json()
        assert base["status"] == "sat" and other["status"] == "sat"
        return base["fingerprint"], other["fingerprint"]

    def test_compare_full_report(self, client):
        fp_base, fp_other = self._two_fingerprints(client)
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base, "other_fingerprint": fp_other},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["base"]["fingerprint"] == fp_base
        assert body["other"]["fingerprint"] == fp_other

        comps = body["components"]
        assert [c["name"] for c in comps["upgraded"]] == ["app", "lib"]
        assert comps["upgraded"][0] == {
            "name": "app", "base_version": "1.0.0", "other_version": "2.0.0",
        }
        assert [c["name"] for c in comps["removed"]] == ["legacy"]
        assert comps["added"] == [] and comps["downgraded"] == []

        assert body["edges"]["removed"]
        assert body["edges"]["added"]
        # 每条边都带完整五元组
        edge = body["edges"]["removed"][0]
        assert set(edge) == {
            "source", "source_version", "target", "target_version", "spec"
        }

        affected = {a["component"]: a for a in body["affected"]}
        # app、lib 自身升级，legacy 被移除：均为变化组件，不出现在 affected。
        # platform 直接依赖升级后的 app（距离 1，链头取 app 新版本）。
        assert set(affected) == {"platform"}
        assert affected["platform"]["chain"] == ["app@2.0.0", "platform@1.0.0"]
        assert affected["platform"]["distance"] == 1
        assert "app" not in affected
        assert "lib" not in affected and "legacy" not in affected

    def test_compare_indirect_chain_distance_two(self, client):
        # app 只有一个版本（不变化），lib 因根要求变化而升级；
        # platform 经未变化的 app 间接依赖 lib，链距离应为 2。
        def payload(lib_spec):
            return {
                "components": [
                    {"name": "platform", "candidates": [
                        {"version": "1.0.0",
                         "dependencies": [{"name": "app", "spec": "*"}]}]},
                    {"name": "app", "candidates": [
                        {"version": "3.0.0",
                         "dependencies": [{"name": "lib", "spec": "*"}]}]},
                    {"name": "lib", "candidates": [
                        {"version": "1.0.0"}, {"version": "2.0.0"}]},
                ],
                "requirements": [
                    {"name": "platform", "spec": "*"},
                    {"name": "lib", "spec": lib_spec},
                ],
            }

        fp_base = client.post("/api/v1/resolve", json=payload("^1.0.0")).json()[
            "fingerprint"
        ]
        fp_other = client.post("/api/v1/resolve", json=payload("^2.0.0")).json()[
            "fingerprint"
        ]
        body = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base, "other_fingerprint": fp_other},
        ).json()
        affected = {a["component"]: a for a in body["affected"]}
        assert set(affected) == {"app", "platform"}
        assert affected["app"]["chain"] == ["lib@2.0.0", "app@3.0.0"]
        assert affected["platform"]["chain"] == [
            "lib@2.0.0", "app@3.0.0", "platform@1.0.0"
        ]
        assert affected["platform"]["distance"] == 2
        assert [a["component"] for a in body["affected"]] == ["app", "platform"]

    def test_compare_same_fingerprint_is_empty_diff(self, client):
        fp, _ = self._two_fingerprints(client)
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp, "other_fingerprint": fp},
        )
        assert r.status_code == 200
        body = r.json()
        for bucket in body["components"].values():
            assert bucket == []
        assert body["edges"] == {"added": [], "removed": []}
        assert body["affected"] == []

    def test_compare_symmetric_pair_order_swapped(self, client):
        fp_base, fp_other = self._two_fingerprints(client)
        forward = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base, "other_fingerprint": fp_other},
        ).json()
        reverse = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_other, "other_fingerprint": fp_base},
        ).json()
        # 反向比较：升级变降级、新增边变移除边
        assert [c["name"] for c in reverse["components"]["upgraded"]] == []
        assert sorted(c["name"] for c in reverse["components"]["downgraded"]) == [
            "app", "lib"
        ]
        assert [c["name"] for c in reverse["components"]["added"]] == ["legacy"]
        assert reverse["edges"]["added"] == forward["edges"]["removed"]
        assert reverse["edges"]["removed"] == forward["edges"]["added"]

    def test_compare_unknown_base_fingerprint_404(self, client):
        _, fp_other = self._two_fingerprints(client)
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": "0" * 64, "other_fingerprint": fp_other},
        )
        assert r.status_code == 404
        assert "基线" in r.json()["detail"]

    def test_compare_unknown_other_fingerprint_404(self, client):
        fp_base, _ = self._two_fingerprints(client)
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base, "other_fingerprint": "f" * 64},
        )
        assert r.status_code == 404
        assert "待对比" in r.json()["detail"]

    def test_compare_both_unknown_404(self, client):
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": "a" * 64, "other_fingerprint": "b" * 64},
        )
        assert r.status_code == 404

    def test_compare_unsat_base_409(self, client):
        fp_base = client.post("/api/v1/resolve", json={
            "components": [{"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "2.0.0"}]}],
            "requirements": [
                {"name": "lib", "spec": "^1.0.0"},
                {"name": "lib", "spec": "^2.0.0"},
            ],
        }).json()["fingerprint"]
        _, fp_other = self._two_fingerprints(client)
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base, "other_fingerprint": fp_other},
        )
        assert r.status_code == 409
        assert "unsat" in r.json()["detail"]

    def test_compare_unsat_other_409(self, client):
        fp_base, _ = self._two_fingerprints(client)
        fp_unsat = client.post("/api/v1/resolve", json={
            "components": [{"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "2.0.0"}]}],
            "requirements": [
                {"name": "lib", "spec": "^1.0.0"},
                {"name": "lib", "spec": "^2.0.0"},
            ],
        }).json()["fingerprint"]
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base, "other_fingerprint": fp_unsat},
        )
        assert r.status_code == 409

    def test_compare_both_unsat_409(self, client):
        unsat_payload = {
            "components": [{"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "2.0.0"}]}],
            "requirements": [
                {"name": "lib", "spec": "^1.0.0"},
                {"name": "lib", "spec": "^2.0.0"},
            ],
        }
        fp = client.post("/api/v1/resolve", json=unsat_payload).json()["fingerprint"]
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp, "other_fingerprint": fp},
        )
        assert r.status_code == 409

    def test_compare_404_takes_precedence_over_409(self, client):
        # base 存在（sat），other 不存在：即使库里有 unsat 结果也不影响
        fp_base, _ = self._two_fingerprints(client)
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base, "other_fingerprint": "1" * 64},
        )
        assert r.status_code == 404

    def test_compare_missing_field_422(self, client):
        fp_base, _ = self._two_fingerprints(client)
        r = client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": fp_base},
        )
        assert r.status_code == 422

    def test_compare_stable_ordering_on_repeat(self, client):
        fp_base, fp_other = self._two_fingerprints(client)
        payload = {"base_fingerprint": fp_base, "other_fingerprint": fp_other}
        first = client.post("/api/v1/resolutions/compare", json=payload).json()
        second = client.post("/api/v1/resolutions/compare", json=payload).json()
        assert first == second
        # 边列表有序
        for key in ("added", "removed"):
            edges = first["edges"][key]
            tuples = [
                (e["source"], e["source_version"], e["target"],
                 e["target_version"], e["spec"])
                for e in edges
            ]
            assert tuples == sorted(tuples)

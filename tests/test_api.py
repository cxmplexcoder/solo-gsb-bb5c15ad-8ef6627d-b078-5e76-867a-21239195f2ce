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


def _base_payload():
    """suite -> platform -> web -> {lib ^1, util}；lib 选 1.0.0。"""
    return {
        "components": [
            {"name": "suite", "candidates": [
                {"version": "1.0.0", "dependencies": [{"name": "platform", "spec": "*"}]}]},
            {"name": "platform", "candidates": [
                {"version": "1.0.0", "dependencies": [{"name": "web", "spec": "^1.0.0"}]}]},
            {"name": "web", "candidates": [
                {"version": "1.0.0", "dependencies": [
                    {"name": "lib", "spec": "^1.0.0"},
                    {"name": "util", "spec": "*"}]}]},
            {"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "2.0.0"}]},
            {"name": "util", "candidates": [{"version": "1.0.0"}]},
        ],
        "requirements": [{"name": "suite", "spec": "*"}],
    }


def _target_payload():
    """platform 改要求 web ^2；web 2.0.0 改要求 lib ^2 且不再依赖 util。"""
    payload = _base_payload()
    comps = {c["name"]: c for c in payload["components"]}
    comps["platform"]["candidates"] = [
        {"version": "1.0.0", "dependencies": [{"name": "web", "spec": "^2.0.0"}]}
    ]
    comps["web"]["candidates"].append(
        {"version": "2.0.0", "dependencies": [{"name": "lib", "spec": "^2.0.0"}]}
    )
    return payload


class TestCompare:
    def _resolve(self, client, payload):
        r = client.post("/api/v1/resolve", json=payload)
        assert r.status_code == 201
        return r.json()["fingerprint"]

    def _compare(self, client, base_fp, target_fp):
        return client.post(
            "/api/v1/resolutions/compare",
            json={"base_fingerprint": base_fp, "target_fingerprint": target_fp},
        )

    def test_full_diff(self, client):
        base_fp = self._resolve(client, _base_payload())
        target_fp = self._resolve(client, _target_payload())

        r = self._compare(client, base_fp, target_fp)
        assert r.status_code == 200
        body = r.json()
        assert body["base_fingerprint"] == base_fp
        assert body["target_fingerprint"] == target_fp

        comps = body["components"]
        assert comps["added"] == []
        assert comps["removed"] == [{"name": "util", "version": "1.0.0"}]
        assert {(c["name"], c["version_from"], c["version_to"])
                for c in comps["upgraded"]} == {
            ("lib", "1.0.0", "2.0.0"),
            ("web", "1.0.0", "2.0.0"),
        }
        assert comps["downgraded"] == [] and comps["other"] == []

        assert [(e["source"], e["target"], e["spec"])
                for e in body["edges"]["added"]] == [
            ("platform", "web", "^2.0.0"),
            ("web", "lib", "^2.0.0"),
        ]
        assert [(e["source"], e["target"], e["spec"])
                for e in body["edges"]["removed"]] == [
            ("platform", "web", "^1.0.0"),
            ("web", "lib", "^1.0.0"),
            ("web", "util", "*"),
        ]

        impacted = {e["component"]: e["source_chain"] for e in body["impacted"]}
        # 变化组件（lib/web/util）自身不计入受影响
        assert set(impacted) == {"platform", "suite"}
        assert impacted["platform"] == ["web", "platform"]
        assert impacted["suite"] == ["web", "platform", "suite"]

    def test_direction_swaps_added_and_removed(self, client):
        base_fp = self._resolve(client, _base_payload())
        target_fp = self._resolve(client, _target_payload())

        reverse = self._compare(client, target_fp, base_fp).json()
        assert reverse["components"]["added"] == [{"name": "util", "version": "1.0.0"}]
        assert reverse["components"]["removed"] == []
        assert {(c["name"]) for c in reverse["components"]["downgraded"]} == {"lib", "web"}

    def test_identical_fingerprints_yield_empty_diff(self, client):
        fp = self._resolve(client, _base_payload())
        r = self._compare(client, fp, fp)
        assert r.status_code == 200
        body = r.json()
        comps = body["components"]
        assert all(comps[k] == [] for k in
                   ("added", "removed", "upgraded", "downgraded", "other"))
        assert body["edges"] == {"added": [], "removed": []}
        assert body["impacted"] == []

    def test_unknown_fingerprint_404(self, client):
        fp = self._resolve(client, _base_payload())
        missing = "0" * 64

        r1 = self._compare(client, missing, fp)
        assert r1.status_code == 404
        assert missing in r1.json()["detail"]

        r2 = self._compare(client, fp, missing)
        assert r2.status_code == 404

        r3 = self._compare(client, missing, missing)
        assert r3.status_code == 404

    def test_unsat_side_returns_409(self, client):
        sat_fp = self._resolve(client, _base_payload())
        unsat = client.post("/api/v1/resolve", json={
            "components": [{"name": "lib", "candidates": [
                {"version": "1.0.0"}, {"version": "2.0.0"}]}],
            "requirements": [
                {"name": "lib", "spec": "^1.0.0"},
                {"name": "lib", "spec": "^2.0.0"},
            ],
        }).json()
        unsat_fp = unsat["fingerprint"]
        assert unsat["status"] == "unsat"

        r = self._compare(client, unsat_fp, sat_fp)
        assert r.status_code == 409
        assert unsat_fp in r.json()["detail"]

        r2 = self._compare(client, sat_fp, unsat_fp)
        assert r2.status_code == 409

    def test_unsat_and_missing_checks_existence_first(self, client):
        sat_fp = self._resolve(client, _base_payload())
        unsat = client.post("/api/v1/resolve", json={
            "components": [{"name": "lib", "candidates": [{"version": "1.0.0"}]}],
            "requirements": [{"name": "lib", "spec": "^2.0.0"}],
        }).json()["fingerprint"]
        missing = "f" * 64
        # 基准不存在优先报 404，即使对照侧 unsat
        r = self._compare(client, missing, unsat)
        assert r.status_code == 404
        # 基准存在但 unsat、对照不存在：先两侧都存在再判状态 => 404
        r2 = self._compare(client, unsat, missing)
        assert r2.status_code == 404
        assert sat_fp  # 仅为保持夹具可读性

    def test_malformed_fingerprint_422(self, client):
        fp = self._resolve(client, _base_payload())
        r = self._compare(client, "not-hex", fp)
        assert r.status_code == 422
        r2 = self._compare(client, fp, "a" * 63)
        assert r2.status_code == 422

    def test_missing_field_422(self, client):
        fp = self._resolve(client, _base_payload())
        r = client.post("/api/v1/resolutions/compare", json={"base_fingerprint": fp})
        assert r.status_code == 422

    def test_result_is_stable_across_calls(self, client):
        base_fp = self._resolve(client, _base_payload())
        target_fp = self._resolve(client, _target_payload())
        first = self._compare(client, base_fp, target_fp).json()
        second = self._compare(client, base_fp, target_fp).json()
        assert first == second
        # impacted 按组件名排序；各分类桶内部稳定有序
        names = [e["component"] for e in first["impacted"]]
        assert names == sorted(names)

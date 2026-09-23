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

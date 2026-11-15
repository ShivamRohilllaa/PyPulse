import asyncio
import os
from types import SimpleNamespace

import django
from django.test import Client, override_settings
from fastapi import FastAPI
from fastapi.testclient import TestClient
from flask import Flask
from pypulse_agent.fastapi import PulseMiddleware
from pypulse_agent.flask import instrument


def report(dashboard, service_id):
    result = dashboard.get(f"/api/python/services/{service_id}/report?hours=1")
    result.raise_for_status()
    return result.json()


def test_fastapi_routes_errors_and_budget(dashboard, service):
    service_id, agent = service("FastAPI")
    assert report(dashboard, service_id)["availability"] is None
    app = FastAPI()
    app.add_middleware(PulseMiddleware, agent=agent)

    @app.get("/orders/{order_id}")
    def order(order_id: int):
        if order_id == 3:
            raise RuntimeError("sample failure")
        return {"id": order_id}

    with TestClient(app, raise_server_exceptions=False) as client:
        assert [client.get(f"/orders/{i}?private=abc").status_code for i in (1, 2, 3)] == [200, 200, 500]
        assert client.get("/unknown-personal-path").status_code == 404
    assert agent.flush()
    data = report(dashboard, service_id)
    assert (data["count"], data["errors"], data["availability"]) == (4, 1, 75)
    assert round(data["budgetUsed"]) == 2500
    orders, unknown = data["routes"]
    assert (orders["route"], orders["count"], orders["serverErrors"]) == ("/orders/{order_id}", 3, 1)
    assert (unknown["route"], unknown["clientErrors"]) == ("(unmatched)", 1)


def test_flask_routes_and_error_handler(dashboard, service):
    service_id, agent = service("Flask")
    app = Flask(__name__)
    assert instrument(app, agent) is instrument(app, agent)

    @app.get("/orders/<int:order_id>")
    def order(order_id):
        return {"id": order_id}, 503 if order_id == 2 else 200

    client = app.test_client()
    assert client.get("/orders/1").status_code == 200
    assert client.get("/orders/2").status_code == 503
    assert client.get("/missing").status_code == 404
    assert agent.flush()
    row = report(dashboard, service_id)["routes"][0]
    assert (row["route"], row["count"], row["serverErrors"]) == ("/orders/<int:order_id>", 2, 1)
    assert row["sqlQueriesPerRequest"] is None


def test_drf_database_and_async_coverage(dashboard, service):
    service_id, agent = service("Django / DRF")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "examples.python.django_settings")
    django.setup()
    from pypulse_agent.django import middleware

    with override_settings(PYPULSE_AGENT=agent):
        client = Client()
        assert client.get("/orders/12").json() == {"id": 12, "state": "ready"}
        assert client.get("/orders/25").status_code == 200
        assert client.get("/missing-with-id-12").status_code == 404

        async def async_view(request):
            return SimpleNamespace(status_code=200)

        request = SimpleNamespace(method="GET", resolver_match=SimpleNamespace(route="async/"))
        asyncio.run(middleware(async_view)(request))
    assert agent.flush()
    routes = {r["route"]: r for r in report(dashboard, service_id)["routes"]}
    assert routes["/orders/<int:order_id>"]["sqlQueriesPerRequest"] == 1
    assert routes["/orders/<int:order_id>"]["sqlSamples"] == 2
    assert routes["/async/"]["sqlQueriesPerRequest"] is None
    assert routes["(unmatched)"]["clientErrors"] == 1


def test_lost_ack_and_merged_histograms(dashboard, service, monkeypatch):
    service_id, agent = service("FastAPI")
    original_send = agent._send

    def lost_ack(batch):
        assert original_send(batch)
        raise TimeoutError()

    monkeypatch.setattr(agent, "_send", lost_ack)
    for _ in range(19):
        agent.record("GET", "/sample", 200, 5)
    assert not agent.flush()
    monkeypatch.setattr(agent, "_send", original_send)
    agent.record("GET", "/sample", 500, 15000)
    assert agent.flush()
    data = report(dashboard, service_id)
    assert (data["count"], data["errors"], data["routes"][0]["p95UpperMs"]) == (20, 1, 5)


def test_token_scope_rotation_and_validation(dashboard, service):
    service_id, agent = service("Flask")
    import httpx

    with httpx.Client(base_url=dashboard.base_url, headers={"Authorization": "Bearer " + agent.token}) as sender:
        assert sender.get("/api/python/services").status_code == 401
        assert sender.post("/api/python/ingest", json={"routes": []}).status_code == 400
        changed = dashboard.put(f"/api/python/services/{service_id}/goal", json={"target": 99.95})
        assert changed.json()["target"] == 99.95
        rotated = dashboard.post(f"/api/python/services/{service_id}/rotate").json()
        assert rotated["token"] != agent.token
        assert sender.post("/api/python/ingest", json={}).status_code == 401
        assert dashboard.delete(f"/api/python/services/{service_id}").is_success
        assert dashboard.get(f"/api/python/services/{service_id}/report").status_code == 404

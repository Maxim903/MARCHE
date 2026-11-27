"""Tests du contrôle de coûts du client DataForSEO (sans réseau ni GCP)."""

import pytest
import requests

from app.dataforseo_client import BudgetExceeded, DataForSEO, DataForSEOError


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, url, json=None, auth=None, timeout=None):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def ok(cost):
    return FakeResponse({"status_code": 20000, "cost": cost,
                         "tasks": [{"status_code": 20000, "result": [{"items": []}]}]})


def make_client(responses, max_cost=1.0):
    costs, raws = [], []
    api = DataForSEO(
        max_cost_usd=max_cost,
        auth=("login", "password"),
        session=FakeSession(responses),
        cost_logger=costs.append,
        raw_store=lambda path, batch, data, meta: raws.append((path, batch, data)) or f"gs://b/{batch}",
        retries=3,
        sleep=lambda s: None,
    )
    return api, costs, raws


def test_cost_is_accumulated_logged_and_raw_stored_first():
    api, costs, raws = make_client([ok(0.2), ok(0.3)])
    r1 = api.post("serp/google/organic/live/advanced", [{"keyword": "x"}], "FR")
    api.post("serp/google/organic/live/advanced", [{"keyword": "y"}], "FR")
    assert api.spent == pytest.approx(0.5)
    assert [c["cost_usd"] for c in costs] == [0.2, 0.3]
    assert costs[-1]["cumulative_cost_usd"] == pytest.approx(0.5)
    assert costs[0]["endpoint"] == "serp/google/organic/live/advanced"
    assert costs[0]["country_code"] == "FR"
    assert len(raws) == 2 and r1.raw_uri.startswith("gs://")


def test_budget_exceeded_stops_next_call():
    api, costs, _ = make_client([ok(0.6), ok(0.6), ok(0.6)], max_cost=1.0)
    api.post("serp/google/organic/live/advanced", [{}], "FR")
    api.post("serp/google/organic/live/advanced", [{}], "FR")  # 1,2 > plafond : payé, conservé
    assert api.exhausted
    with pytest.raises(BudgetExceeded):
        api.post("serp/google/organic/live/advanced", [{}], "FR")
    assert api.session.calls == 2  # aucun appel envoyé après le plafond


def test_estimated_cost_blocks_call_before_sending():
    api, _, _ = make_client([ok(0.1)], max_cost=0.10)
    with pytest.raises(BudgetExceeded):
        api.post("dataforseo_labs/google/keyword_ideas/live", [{}], "FR")  # estimation 0,15 > 0,10
    assert api.session.calls == 0


def test_retry_on_network_and_5xx_then_success():
    api, costs, _ = make_client([requests.ConnectionError("boom"), FakeResponse({}, 503), ok(0.05)])
    res = api.post("serp/google/organic/live/advanced", [{}], "US")
    assert res.cost_usd == 0.05 and api.session.calls == 3
    assert len(costs) == 1


def test_non_retryable_error_raises_immediately():
    api, _, _ = make_client([FakeResponse({"status_code": 40100, "status_message": "auth", "cost": 0})])
    with pytest.raises(DataForSEOError):
        api.post("serp/google/organic/live/advanced", [{}], "FR")
    assert api.session.calls == 1


def test_http_401_not_retried():
    api, _, _ = make_client([FakeResponse("unauthorized", 401)])
    with pytest.raises(DataForSEOError):
        api.post("serp/google/organic/live/advanced", [{}], "FR")
    assert api.session.calls == 1


def test_cost_logged_even_on_global_error():
    api, costs, raws = make_client([FakeResponse({"status_code": 40501, "status_message": "bad", "cost": 0.01})])
    with pytest.raises(DataForSEOError):
        api.post("serp/google/organic/live/advanced", [{}], "FR")
    assert api.spent == pytest.approx(0.01) and len(costs) == 1 and raws == []

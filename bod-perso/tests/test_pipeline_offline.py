"""Test d'intégration hors-ligne : collect -> cluster -> opportunities -> simulate,
avec BigQuery, Cloud Storage, DataForSEO et Vertex AI simulés en mémoire."""

import json

import numpy as np
import pandas as pd
import pytest

import app.bq as bq
from app import cluster, collect, opportunities, simulate
from app.bq import to_jsonable
from app.config import get_settings
from app.dataforseo_client import DataForSEO


class Store:
    def __init__(self):
        self.tables: dict[str, list[dict]] = {}

    def load_rows(self, table, rows):
        rows = json.loads(json.dumps(bq.clean_rows(rows)))  # sérialisable comme pour BigQuery
        self.tables.setdefault(table, []).extend(rows)
        return len(rows)

    def df(self, table):
        return pd.DataFrame(self.tables.get(table, []))


def _labs_response(keywords):
    items = [{"keyword": kw, "keyword_info": {"search_volume": vol, "cpc": cpc, "competition": 0.3},
              "search_intent_info": {"main_intent": "commercial"}} for kw, vol, cpc in keywords]
    return {"status_code": 20000, "cost": 0.02, "tasks": [{"status_code": 20000, "result": [{"items": items}]}]}


class FakeSession:
    def __init__(self, payload):
        self.payload = payload

    def post(self, url, json=None, auth=None, timeout=None):
        class R:
            status_code = 200
            text = ""

            def json(_):
                return self.payload
        return R()


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "test-proj")
    get_settings.cache_clear()
    store = Store()
    monkeypatch.setattr(bq, "load_rows", store.load_rows)
    monkeypatch.setattr(collect, "load_rows", store.load_rows)
    yield store
    get_settings.cache_clear()


def test_offline_pipeline(env, monkeypatch):
    store = env
    a = [(f"nettoyage canape {i}", 500 + i, 1.2) for i in range(10)]
    b = [(f"logiciel facturation {i}", 300 + i, 2.0) for i in range(10)]
    api = DataForSEO(max_cost_usd=1.0, auth=("u", "p"), session=FakeSession(_labs_response(a + b)),
                     cost_logger=lambda row: store.load_rows("api_costs", [row]),
                     raw_store=lambda *args: "gs://test-proj-raw/raw/x.json", sleep=lambda s: None)

    # 1) collect
    assert collect.run(api, ["FR"]) == {"FR": 20}
    kw = store.df("keywords_raw")
    assert set(kw["data_kind"]) == {"api_estimate"} and kw["raw_uri"].notna().all()
    assert store.df("api_costs")["cost_usd"].sum() == pytest.approx(0.02)

    # 2) cluster (embeddings simulés : deux groupes nets)
    monkeypatch.setattr(bq, "query", lambda sql, params=None: store.df("keywords_raw"))
    monkeypatch.setattr(cluster, "embed", lambda texts: np.array(
        [[10.0, 0, 0] if "canape" in t else [0, 10.0, 0] for t in texts]) + np.random.default_rng(0).normal(0, .01, (len(texts), 3)))
    assert cluster.run(["FR"], "r1") == {"FR": 2}

    # 3) opportunities (Gemini simulé)
    def fake_build(client, c, model):
        return opportunities.sanitize(opportunities.OpportunityLLM.model_validate({
            "need": f"besoin {c['label']}", "segment": "particuliers", "intent_type": "commercial",
            "offers": [{"name": f"offre {c['label']}", "business_model": "service", "sales_motion": "direct",
                        "price_hypothesis_eur": 90, "margin_rate_hypothesis": 0.6,
                        "lead_to_sale_hypothesis": 1, "ltv_months": 1, "delivery_effort": "low"}],
            "risks": [], "regulatory_flags": [], "confidence": "medium"})), "{}"

    monkeypatch.setattr(bq, "query", lambda sql, params=None: store.df("clusters"))
    monkeypatch.setattr(bq, "upload_json", lambda *a, **k: "gs://x")
    monkeypatch.setattr(opportunities, "_client", lambda: None)
    monkeypatch.setattr(opportunities, "build_opportunity", fake_build)
    assert opportunities.run(["FR"], "r1") == 2

    # 4) simulate (jointure opportunities x clusters faite en pandas)
    joined = store.df("opportunities").merge(
        store.df("clusters")[["cluster_id", "total_volume", "avg_cpc", "weighted_cpc", "avg_competition",
                              "trend_yoy", "commercial_share", "n_keywords"]], on="cluster_id")
    for col in ("deliverable", "price_override_eur", "margin_rate_override", "lead_to_sale_override",
                "ltv_months_override"):
        joined[col] = None
    monkeypatch.setattr(bq, "query", lambda sql, params=None: joined)
    assert simulate.run(["FR"], "r1") == 2
    sc = store.df("scenarios")
    assert len(sc) == 6 and set(sc["data_kind"]) == {"model_assumption"}
    scores = store.df("opportunity_scores")
    assert scores["mos"].between(0, 100).all() and not scores["eliminated"].any()


def test_to_jsonable_arrays():
    assert to_jsonable(np.array(["x"])) == ["x"]
    assert to_jsonable({"a": np.int64(2), "b": float("nan")}) == {"a": 2, "b": None}

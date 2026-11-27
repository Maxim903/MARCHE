"""Tests des fonctions pures de parsing / filtrage (collect, cluster, serp, opportunities)."""

import numpy as np
import pandas as pd
import pytest

from app.cluster import cluster_keywords, filter_keywords
from app.collect import compute_yoy_trend, parse_keyword_items
from app.opportunities import OpportunityLLM, opportunity_id, sanitize, to_rows
from app.serp import extract_prices, parse_serp


def test_parse_keyword_ideas_dedup_and_traceability():
    data = {"tasks": [{"status_code": 20000, "result": [{"items": [
        {"keyword": "Nettoyage Canape", "keyword_info": {"search_volume": 900, "cpc": 1.4, "competition": 0.3},
         "search_intent_info": {"main_intent": "commercial"}},
        {"keyword": "nettoyage canape", "keyword_info": {"search_volume": 900}},
        {"keyword_data": {"keyword": "prix nettoyage canape", "keyword_info": {"search_volume": 200, "cpc": 2.0},
                          "search_intent_info": {"main_intent": "transactional"}}},
    ]}]}]}
    rows = parse_keyword_items(data, country="FR", endpoint_name="keyword_ideas", run_id="r1",
                               observed_at="2026-11-27T00:00:00+00:00")
    assert [r["keyword"] for r in rows] == ["nettoyage canape", "prix nettoyage canape"]
    r = rows[0]
    assert (r["location_code"], r["language_code"], r["intent"]) == (2250, "fr", "commercial")
    assert r["data_kind"] == "api_estimate" and r["source"] == "dataforseo" and r["run_id"] == "r1"


def test_yoy_trend():
    assert compute_yoy_trend({"search_volume_trend": {"yearly": 25}}) == pytest.approx(0.25)
    monthly = [{"year": 2026, "month": m, "search_volume": 120} for m in (8, 9, 10)] + \
              [{"year": 2025, "month": m, "search_volume": 100} for m in (8, 9, 10)]
    assert compute_yoy_trend({"monthly_searches": monthly}) == pytest.approx(0.2)
    assert compute_yoy_trend({"monthly_searches": monthly[:3]}) is None


def test_filter_keywords():
    df = pd.DataFrame([
        {"keyword": " Devis Plombier ", "search_volume": 100, "cpc": 3.0, "intent": "commercial"},
        {"keyword": "devis plombier", "search_volume": 100, "cpc": 3.0, "intent": "commercial"},
        {"keyword": "plombier gratuit", "search_volume": 100, "cpc": 1.0, "intent": "commercial"},
        {"keyword": "amazon login", "search_volume": 100, "cpc": 1.0, "intent": "navigational"},
        {"keyword": "comment deboucher", "search_volume": 500, "cpc": 0.5, "intent": "informational"},
        {"keyword": "plombier urgence", "search_volume": 300, "cpc": None, "intent": "transactional"},
    ])
    out = filter_keywords(df, exclude_terms=["gratuit", "login"])
    assert out["keyword"].tolist() == ["devis plombier"]
    out2 = filter_keywords(df, exclude_terms=["gratuit", "login"], include_informational=True)
    assert "comment deboucher" in out2["keyword"].tolist()


def test_cluster_keywords_on_synthetic_embeddings():
    rng = np.random.default_rng(0)
    centers = np.eye(8)[:2] * 10
    vecs = np.vstack([centers[0] + rng.normal(0, 0.1, (12, 8)), centers[1] + rng.normal(0, 0.1, (12, 8))])
    df = pd.DataFrame({
        "keyword": [f"a{i}" for i in range(12)] + [f"b{i}" for i in range(12)],
        "search_volume": list(range(100, 112)) + list(range(10, 22)),
        "cpc": [1.0] * 12 + [2.0] * 12,
        "competition": [0.2] * 24,
        "trend_yoy": [0.1] * 24,
        "intent": ["commercial"] * 24,
    })
    members, agg = cluster_keywords(df, vecs, min_cluster_size=5)
    assert len(agg) == 2
    assert sorted(agg["n_keywords"]) == [12, 12]
    a = agg[agg["label"] == "a11"].iloc[0]
    assert a["total_volume"] == sum(range(100, 112))
    assert a["top_keywords"][0] == "a11" and len(a["top_keywords"]) == 10
    assert a["commercial_share"] == 1.0


def test_parse_serp_indicators():
    data = {"tasks": [{"result": [{"items": [
        {"type": "paid", "domain": "www.pro-net.fr", "title": "Nettoyage canapé dès 79 €", "description": ""},
        {"type": "organic", "rank_group": 1, "domain": "www.amazon.fr", "url": "https://www.amazon.fr/x",
         "title": "Kit nettoyage", "description": "29,90 €"},
        {"type": "organic", "rank_group": 2, "domain": "fr.wikipedia.org", "title": "Canapé", "description": ""},
        {"type": "organic", "rank_group": 3, "domain": "blog.fr", "title": "Comment nettoyer un canapé",
         "description": ""},
        {"type": "people_also_ask", "items": [{}, {}, {}]},
    ]}]}]}
    out = parse_serp(data)
    assert out["n_paid"] == 1 and out["n_organic"] == 3 and out["paa_count"] == 3
    assert out["marketplaces_top10"] == ["amazon.fr"]
    assert out["informational_share"] == pytest.approx(2 / 3, abs=1e-3)
    assert sorted(out["price_mentions"]) == [29.9, 79.0]


def test_extract_prices():
    assert extract_prices("From $49.99 or 120 EUR") == [49.99, 120.0]


def test_opportunity_rows_are_model_assumptions():
    llm = OpportunityLLM.model_validate({
        "need": "canapé propre sans effort", "segment": "particuliers", "intent_type": "transactional",
        "offers": [
            {"name": "Nettoyage à domicile", "business_model": "service", "sales_motion": "lead",
             "price_hypothesis_eur": 120, "margin_rate_hypothesis": 0.6, "lead_to_sale_hypothesis": 0.3,
             "ltv_months": 5, "delivery_effort": "medium"},
            {"name": "Kit", "business_model": "one_time_product", "sales_motion": "direct",
             "price_hypothesis_eur": 0, "margin_rate_hypothesis": 0.4, "lead_to_sale_hypothesis": 0.2,
             "ltv_months": 1, "delivery_effort": "low"},
        ],
        "risks": ["saisonnalité"], "regulatory_flags": [], "confidence": "medium",
    })
    llm = sanitize(llm)
    assert len(llm.offers) == 1  # offre à prix nul écartée
    assert llm.offers[0].ltv_months == 1.0  # pas un abonnement
    rows = to_rows({"cluster_id": "CL-1", "country_code": "FR"}, llm, run_id="r", model="m",
                   generated_at="2026-11-27T00:00:00+00:00")
    assert rows[0]["data_kind"] == "model_assumption"
    assert rows[0]["opportunity_id"] == opportunity_id("CL-1", "Nettoyage à domicile")

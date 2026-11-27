"""Tests unitaires du simulateur : CPA, ROAS, contribution, breakeven_cr,
scénarios, scores et conditions éliminatoires."""

import math

import pandas as pd
import pytest

from app.simulate import (
    breakeven_cr,
    budget_outlook,
    compute_scores,
    contribution_per_sale,
    cpa,
    elimination_reasons,
    ltv,
    max_acceptable_cpa,
    net_price,
    percentile_score,
    prepare,
    profit_per_sale,
    readiness_issues,
    roas,
    rule_of_three_upper_bound,
    scenario,
    three_scenarios,
)


# ---------------------------------------------------------------- formules
class TestFormulas:
    def test_cpa_direct(self):
        # CPC 1,50 / 3 % = 50 EUR
        assert cpa(1.5, 0.03) == pytest.approx(50.0)

    def test_cpa_with_lead_to_sale(self):
        # Le CPA d'un client payant intègre le taux lead -> vente
        assert cpa(2.0, 0.04, 0.25) == pytest.approx(200.0)

    @pytest.mark.parametrize("cr", [0, -0.1, 1.5])
    def test_cpa_rejects_invalid_rate(self, cr):
        with pytest.raises(ValueError):
            cpa(1.0, cr)

    def test_cpa_rejects_negative_cpc(self):
        with pytest.raises(ValueError):
            cpa(-1.0, 0.03)

    def test_roas(self):
        assert roas(500.0, 200.0) == pytest.approx(2.5)
        assert roas(100.0, 0.0) is None

    def test_net_price_and_contribution(self):
        assert net_price(100.0, 0.05) == pytest.approx(95.0)
        # Prix net après remboursements x taux de marge
        assert contribution_per_sale(100.0, 0.4, 0.05) == pytest.approx(38.0)
        # Abonnement : x durée de vie
        assert contribution_per_sale(20.0, 0.8, 0.0, ltv_months=10) == pytest.approx(160.0)

    def test_contribution_rejects_invalid_ltv(self):
        with pytest.raises(ValueError):
            contribution_per_sale(100.0, 0.5, ltv_months=0.5)

    def test_ltv(self):
        assert ltv(30.0, 0.7, 12) == pytest.approx(252.0)

    def test_profit_and_max_cpa(self):
        assert profit_per_sale(40.0, 25.0) == pytest.approx(15.0)
        assert max_acceptable_cpa(40.0) == pytest.approx(40.0)

    def test_breakeven_cr_guide_example(self):
        # Guide : CPC 2 EUR, contribution 40 EUR -> au moins 5 % de conversion
        assert breakeven_cr(2.0, 40.0) == pytest.approx(0.05)

    def test_breakeven_cr_with_lead_to_sale(self):
        assert breakeven_cr(2.0, 40.0, 0.5) == pytest.approx(0.10)

    def test_breakeven_cr_non_positive_contribution(self):
        assert breakeven_cr(2.0, 0.0) is None
        assert breakeven_cr(2.0, -10.0) is None

    def test_breakeven_is_consistent_with_zero_profit(self):
        cpc, contribution, l2s = 1.2, 60.0, 0.3
        be = breakeven_cr(cpc, contribution, l2s)
        assert profit_per_sale(contribution, cpa(cpc, be, l2s)) == pytest.approx(0.0, abs=1e-9)

    def test_rule_of_three(self):
        assert rule_of_three_upper_bound(100) == pytest.approx(0.03)
        assert rule_of_three_upper_bound(150) == pytest.approx(0.02)
        assert rule_of_three_upper_bound(0) is None

    def test_budget_outlook_guide_example(self):
        # Guide : CPC 1,50, 180 EUR -> ~120 clics, 3 à 4 conversions à 3 %
        out = budget_outlook(180, 1.5, 0.03)
        assert out["expected_clicks"] == pytest.approx(120.0)
        assert out["expected_conversions"] == pytest.approx(3.6)


# ---------------------------------------------------------------- scénarios
class TestScenarios:
    def test_scenario_matches_guide_formula(self):
        r = scenario(cpc=2.5, cr=0.03, price=500, margin_rate=0.55, lead_to_sale=0.2, refund_rate=0.05)
        assert r["net_price"] == pytest.approx(475.0)
        assert r["contribution"] == pytest.approx(261.25)
        assert r["cpa"] == pytest.approx(416.67, abs=0.01)
        assert r["profit_per_sale"] == pytest.approx(-155.42, abs=0.01)
        assert r["roas"] == pytest.approx(1.14, abs=0.01)
        assert r["max_cpa"] == pytest.approx(261.25)
        assert r["breakeven_cr"] == pytest.approx(0.0478, abs=1e-4)

    def test_three_scenarios_parameters(self):
        sc = three_scenarios(2.5, 500, 0.55, lead_to_sale=0.2)
        assert set(sc) == {"pessimistic", "central", "optimistic"}
        assert sc["pessimistic"]["cpc"] == pytest.approx(3.25)
        assert sc["central"]["cpc"] == pytest.approx(2.5)
        assert sc["optimistic"]["cpc"] == pytest.approx(2.0)
        assert (sc["pessimistic"]["cr"], sc["central"]["cr"], sc["optimistic"]["cr"]) == (0.01, 0.03, 0.05)
        assert sc["pessimistic"]["refund_rate"] == 0.08
        assert sc["optimistic"]["refund_rate"] == 0.03

    def test_scenarios_are_ordered(self):
        sc = three_scenarios(1.2, 90, 0.6)
        assert sc["pessimistic"]["cpa"] > sc["central"]["cpa"] > sc["optimistic"]["cpa"]
        assert (
            sc["pessimistic"]["profit_per_sale"]
            < sc["central"]["profit_per_sale"]
            < sc["optimistic"]["profit_per_sale"]
        )

    def test_profitable_direct_offer(self):
        # 90 EUR, marge 60 %, CPC 1,20 : central CPA 40 < contribution 51,30
        c = three_scenarios(1.2, 90, 0.6)["central"]
        assert c["cpa"] == pytest.approx(40.0)
        assert c["contribution"] == pytest.approx(51.3)
        assert c["profit_per_sale"] == pytest.approx(11.3)

    def test_subscription_uses_ltv(self):
        one = scenario(1.0, 0.03, 20, 0.8, ltv_months=1)
        sub = scenario(1.0, 0.03, 20, 0.8, ltv_months=12)
        assert sub["contribution"] == pytest.approx(one["contribution"] * 12)
        assert sub["breakeven_cr"] < one["breakeven_cr"]

    def test_zero_margin_has_no_breakeven(self):
        r = scenario(1.0, 0.03, 100, 0.0)
        assert r["breakeven_cr"] is None
        assert r["profit_per_sale"] < 0


# ---------------------------------------------------------------- scores
def _batch():
    df = pd.DataFrame([
        {"opportunity_id": "A", "cluster_id": "c1", "country_code": "FR", "sales_motion": "direct",
         "price_hypothesis_eur": 90.0, "margin_rate_hypothesis": 0.6, "lead_to_sale_hypothesis": 1.0,
         "ltv_months": 1.0, "delivery_effort": "low", "regulatory_flags": [], "confidence": "high",
         "total_volume": 5000, "avg_cpc": 1.3, "weighted_cpc": 1.3, "avg_competition": 0.3,
         "trend_yoy": 0.2, "commercial_share": 0.9, "n_keywords": 40, "deliverable": None},
        {"opportunity_id": "B", "cluster_id": "c2", "country_code": "FR", "sales_motion": "lead",
         "price_hypothesis_eur": 500.0, "margin_rate_hypothesis": 0.55, "lead_to_sale_hypothesis": 0.2,
         "ltv_months": 1.0, "delivery_effort": "medium", "regulatory_flags": [], "confidence": "medium",
         "total_volume": 1500, "avg_cpc": 2.7, "weighted_cpc": 2.7, "avg_competition": 0.7,
         "trend_yoy": None, "commercial_share": 0.6, "n_keywords": 12, "deliverable": None},
        {"opportunity_id": "C", "cluster_id": "c3", "country_code": "FR", "sales_motion": "direct",
         "price_hypothesis_eur": 40.0, "margin_rate_hypothesis": 0.7, "lead_to_sale_hypothesis": 1.0,
         "ltv_months": 1.0, "delivery_effort": "high", "regulatory_flags": ["health"], "confidence": "low",
         "total_volume": 800, "avg_cpc": 0.9, "weighted_cpc": 0.9, "avg_competition": 0.5,
         "trend_yoy": -0.1, "commercial_share": 0.5, "n_keywords": 9, "deliverable": None},
    ])
    return prepare(df, usd_to_eur=1.0)


class TestScoring:
    def test_percentile_score_bounds_and_missing(self):
        s = percentile_score(pd.Series([10, 20, None, 30]))
        assert s.tolist() == [0.0, 50.0, 0.0, 100.0]
        assert percentile_score(pd.Series([10, 20, 30]), higher_is_better=False).tolist() == [100.0, 50.0, 0.0]
        assert percentile_score(pd.Series([7])).tolist() == [50.0]

    def test_compute_scores_range_and_weights(self):
        prepared, sims = _batch()
        scored = compute_scores(prepared)
        for col in ("mos", "pps", "priority_score"):
            assert scored[col].between(0, 100).all()
        assert set(sims) == {"A", "B", "C"}
        a = scored.set_index("opportunity_id").loc["A"]
        # A domine la demande, l'intention et la croissance
        assert a["mos_demand"] == 100.0 and a["mos_intent"] == 100.0 and a["mos_growth"] == 100.0
        assert a["mos"] == scored["mos"].max()

    def test_prepare_converts_cpc_currency(self):
        df = _batch()[0].iloc[:1].copy()
        prepared, _ = prepare(df.drop(columns=["cpc_eur"]), usd_to_eur=0.5)
        assert prepared.loc[0, "cpc_eur"] == pytest.approx(0.65)


class TestElimination:
    def test_eliminatory_conditions(self):
        prepared, _ = _batch()
        rows = {r["opportunity_id"]: r for r in prepared.to_dict("records")}
        assert elimination_reasons(rows["A"]) == []
        reasons_c = elimination_reasons(rows["C"])
        assert "offre_non_livrable" in reasons_c
        assert "restriction_reglementaire:health" in reasons_c

    def test_non_positive_margin_is_eliminated(self):
        row = {"cpc_eur": 1.0, "price_eur": 50.0, "contribution": 0.0, "max_cpa": 0.0,
               "delivery_effort": "low", "regulatory_flags": []}
        reasons = elimination_reasons(row)
        assert "marge_contributive_non_positive" in reasons
        assert "cpa_max_non_defini" in reasons

    def test_human_review_overrides_delivery(self):
        base = {"cpc_eur": 1.0, "price_eur": 90.0, "contribution": 50.0, "max_cpa": 50.0,
                "regulatory_flags": []}
        assert elimination_reasons({**base, "delivery_effort": "high", "deliverable": True}) == []
        assert "offre_non_livrable" in elimination_reasons({**base, "delivery_effort": "low", "deliverable": False})

    def test_missing_data_is_eliminated(self):
        assert "donnees_insuffisantes" in elimination_reasons(
            {"cpc_eur": None, "price_eur": 90.0, "contribution": None, "max_cpa": None}
        )

    def test_test_readiness(self):
        kw = dict(test_budget=180.0, max_cpc=2.0, min_max_cpa=30.0)
        assert readiness_issues({"cpc_eur": 1.2, "max_cpa": 51.3}, **kw) == []
        assert "cpc_superieur_a_2_eur" in readiness_issues({"cpc_eur": 2.5, "max_cpa": 50}, **kw)
        assert "cpa_max_inferieur_a_30_eur" in readiness_issues({"cpc_eur": 1.0, "max_cpa": 20}, **kw)
        assert "budget_test_inferieur_a_3x_cpa_max" in readiness_issues({"cpc_eur": 1.0, "max_cpa": 261}, **kw)

    def test_no_nan_leaks_into_rows(self):
        from app.simulate import build_rows

        prepared, sims = _batch()
        scenarios, scores = build_rows(
            compute_scores(prepared), sims, run_id="t", computed_at="2026-01-01T00:00:00+00:00",
            test_budget=180.0, max_cpc=2.0, min_max_cpa=30.0,
        )
        assert len(scenarios) == 9 and len(scores) == 3
        assert all(r["data_kind"] == "model_assumption" for r in scenarios + scores)
        by_id = {r["opportunity_id"]: r for r in scores}
        assert by_id["A"]["test_ready"] is True
        assert by_id["C"]["eliminated"] is True
        for r in scenarios:
            assert not (isinstance(r["cpa"], float) and math.isnan(r["cpa"]))

"""Étape `simulate` : scoring (MOS / PPS), conditions éliminatoires et
simulation financière en trois scénarios (sections 10 et 12.1 du guide).

Les fonctions de calcul sont pures et testées (tests/test_simulate.py).
Tous les résultats sont des HYPOTHÈSES (data_kind = model_assumption) : les
taux de conversion sont des ordres de grandeur, à remplacer par les mesures
réelles de `bod.experiments` dès le premier test.

Raisonner à l'envers : `breakeven_cr` donne le taux de conversion minimal
nécessaire. S'il est irréaliste pour l'offre, on l'écarte avant de dépenser.
"""

from __future__ import annotations

import logging
import math
import uuid
from typing import Any

import pandas as pd

from app.config import (
    BLOCKING_REGULATORY_FLAGS,
    DATA_KIND_MODEL_ASSUMPTION,
    DEFAULT_LEAD_TO_SALE,
    DEFAULT_LTV_MONTHS,
    DEFAULT_MARGIN_RATE,
    DEFAULT_SCENARIOS,
    MOS_WEIGHTS,
    PPS_WEIGHTS,
)

log = logging.getLogger(__name__)


# ==========================================================================
# 1. Formules essentielles (section 10.2)
# ==========================================================================
def _check_rate(name: str, value: float, *, allow_zero: bool = False) -> None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        raise ValueError(f"{name} manquant")
    if value < 0 or (value == 0 and not allow_zero) or value > 1:
        raise ValueError(f"{name} doit être dans {'[0' if allow_zero else ']0'}, 1] (reçu {value})")


def cpa(cpc: float, cr: float, lead_to_sale: float = 1.0) -> float:
    """Coût d'acquisition d'un client payant = CPC / (taux de conversion x taux lead->vente)."""
    if cpc < 0:
        raise ValueError("cpc négatif")
    _check_rate("cr", cr)
    _check_rate("lead_to_sale", lead_to_sale)
    return cpc / (cr * lead_to_sale)


def roas(revenue: float, ad_spend: float) -> float | None:
    """ROAS = revenu net / dépense publicitaire (None si dépense nulle)."""
    return None if not ad_spend else revenue / ad_spend


def net_price(price: float, refund_rate: float = 0.0) -> float:
    """Prix net après remboursements."""
    _check_rate("refund_rate", refund_rate, allow_zero=True)
    return price * (1 - refund_rate)


def contribution_per_sale(price: float, margin_rate: float, refund_rate: float = 0.0, ltv_months: float = 1.0) -> float:
    """Contribution par vente (ou par client si abonnement) = prix net x taux de marge x durée."""
    _check_rate("margin_rate", margin_rate, allow_zero=True)
    if ltv_months < 1:
        raise ValueError("ltv_months doit être >= 1")
    return net_price(price, refund_rate) * margin_rate * ltv_months


def ltv(arpu: float, margin_rate: float, lifetime_months: float) -> float:
    """LTV d'un abonnement = ARPU x marge x durée de vie moyenne du client."""
    return arpu * margin_rate * lifetime_months


def profit_per_sale(contribution: float, cpa_value: float) -> float:
    """Profit par vente = contribution par vente - CPA."""
    return contribution - cpa_value


def max_acceptable_cpa(contribution: float) -> float:
    """CPA maximal acceptable = contribution par vente (ou LTV x marge)."""
    return contribution


def breakeven_cr(cpc: float, contribution: float, lead_to_sale: float = 1.0) -> float | None:
    """Conversion minimale (seuil de rentabilité) = CPC / (contribution x taux lead->vente).

    None si la contribution n'est pas positive : aucun taux ne rend l'offre rentable.
    """
    _check_rate("lead_to_sale", lead_to_sale)
    if contribution <= 0:
        return None
    return cpc / (contribution * lead_to_sale)


def rule_of_three_upper_bound(n_clicks: int) -> float | None:
    """0 conversion sur n clics => taux réel probablement < 3/n (~95 % de confiance)."""
    return None if n_clicks <= 0 else min(1.0, 3.0 / n_clicks)


# ==========================================================================
# 2. Simulateur trois scénarios (section 10.3)
# ==========================================================================
def scenario(
    cpc: float,
    cr: float,
    price: float,
    margin_rate: float,
    lead_to_sale: float = 1.0,
    refund_rate: float = 0.05,
    ltv_months: float = 1.0,
) -> dict[str, Any]:
    """Un scénario financier, par client acquis. Fidèle à la formule du guide."""
    npx = net_price(price, refund_rate)
    contribution = contribution_per_sale(price, margin_rate, refund_rate, ltv_months)
    cpa_value = cpa(cpc, cr, lead_to_sale)
    r = roas(npx * ltv_months, cpa_value)
    be = breakeven_cr(cpc, contribution, lead_to_sale)
    return {
        "cpc": round(cpc, 4),
        "cr": cr,
        "lead_to_sale": lead_to_sale,
        "refund_rate": refund_rate,
        "price": price,
        "margin_rate": margin_rate,
        "ltv_months": ltv_months,
        "net_price": round(npx, 2),
        "contribution": round(contribution, 2),
        "cpa": round(cpa_value, 2),
        "profit_per_sale": round(contribution - cpa_value, 2),
        "roas": None if r is None else round(r, 2),
        "max_cpa": round(max_acceptable_cpa(contribution), 2),
        "breakeven_cr": None if be is None else round(be, 4),
    }


def three_scenarios(
    cpc: float,
    price: float,
    margin_rate: float,
    lead_to_sale: float = 1.0,
    ltv_months: float = 1.0,
    scenarios: dict[str, dict[str, float]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Pessimiste (CPC x1,3, CR 1 %), central (CPC, 3 %), optimiste (CPC x0,8, 5 %).

    Exemple : service à 500 EUR, marge 55 %, CPC 2,50, 1 lead sur 5 devient client
    -> three_scenarios(2.5, 500, 0.55, lead_to_sale=0.2)
    """
    scenarios = scenarios or DEFAULT_SCENARIOS
    return {
        name: scenario(
            cpc * p["cpc_multiplier"], p["cr"], price, margin_rate,
            lead_to_sale, p["refund_rate"], ltv_months,
        )
        for name, p in scenarios.items()
    }


def budget_outlook(budget: float, cpc: float, cr: float) -> dict[str, float]:
    """Ce qu'un budget de test achète : clics et conversions attendus."""
    clicks = budget / cpc if cpc > 0 else 0.0
    return {"expected_clicks": round(clicks, 1), "expected_conversions": round(clicks * cr, 2)}


# ==========================================================================
# 3. Scores MOS / PPS (section 10.1) — outil de tri, pas modèle prédictif
# ==========================================================================
LEVEL_NUM = {"low": 1.0, "medium": 2.0, "high": 3.0}
FEASIBILITY_NUM = {"low": 3.0, "medium": 2.0, "high": 1.0}  # effort faible = faisable


def percentile_score(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    """Rang percentile 0-100 dans le lot. Valeur manquante -> 0 (on ne récompense
    pas l'absence de donnée). Lot d'un seul élément -> 50 (neutre)."""
    v = pd.to_numeric(values, errors="coerce")
    if not higher_is_better:
        v = -v
    valid = v.notna()
    n = int(valid.sum())
    out = pd.Series(0.0, index=values.index)
    if n == 1:
        out[valid] = 50.0
    elif n > 1:
        ranks = v[valid].rank(method="average")
        out[valid] = 100.0 * (ranks - 1) / (n - 1)
    return out


def weighted_score(components: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    total = sum(weights.values())
    return sum(components[k] * w for k, w in weights.items()) / total


def compute_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute les sous-scores (0-100), `mos` et `pps`.

    Colonnes attendues : total_volume, commercial_share, trend_yoy, avg_competition,
    cpc_eur, price_eur, delivery_effort, unit_contribution, contribution, max_cpa,
    cpa_central, confidence, n_keywords.
    """
    out = df.copy()
    comp = pd.to_numeric(out["avg_competition"], errors="coerce")
    feas = out["delivery_effort"].map(FEASIBILITY_NUM)
    m = pd.DataFrame(index=out.index)
    m["demand"] = percentile_score(out["total_volume"])
    m["intent"] = percentile_score(out["commercial_share"])
    m["growth"] = percentile_score(out["trend_yoy"])
    m["gap"] = percentile_score(comp, higher_is_better=False)
    m["solvency"] = percentile_score(out["cpc_eur"])
    m["size"] = percentile_score(out["total_volume"] * out["price_eur"])
    m["feasibility"] = percentile_score(feas)

    p = pd.DataFrame(index=out.index)
    p["margin"] = percentile_score(out["unit_contribution"])
    headroom = out["max_cpa"] / out["cpa_central"].where(out["cpa_central"] > 0)
    p["cpa_accessibility"] = percentile_score(headroom)
    p["ad_competitiveness"] = (
        percentile_score(out["cpc_eur"], higher_is_better=False)
        + percentile_score(comp, higher_is_better=False)
    ) / 2
    p["ltv"] = percentile_score(out["contribution"])
    conf = out["confidence"].map(LEVEL_NUM).fillna(1.0) / 3.0
    size = (pd.to_numeric(out["n_keywords"], errors="coerce").fillna(0) / 50.0).clip(upper=1.0)
    completeness = out[["total_volume", "cpc_eur", "avg_competition", "trend_yoy"]].notna().mean(axis=1)
    p["reliability"] = percentile_score(0.5 * conf + 0.3 * size + 0.2 * completeness)

    for k in MOS_WEIGHTS:
        out[f"mos_{k}"] = m[k].round(1)
    for k in PPS_WEIGHTS:
        out[f"pps_{k}"] = p[k].round(1)
    out["mos"] = weighted_score(m, MOS_WEIGHTS).round(1)
    out["pps"] = weighted_score(p, PPS_WEIGHTS).round(1)
    out["priority_score"] = (0.5 * out["mos"] + 0.5 * out["pps"]).round(1)
    return out


# ==========================================================================
# 4. Conditions éliminatoires et critères de test (sections 10.1 et 12.1)
# ==========================================================================
def elimination_reasons(row: dict, blocking_flags=BLOCKING_REGULATORY_FLAGS) -> list[str]:
    reasons: list[str] = []
    contribution = row.get("contribution")
    if _missing(row.get("cpc_eur")) or _missing(row.get("price_eur")):
        reasons.append("donnees_insuffisantes")
    if _missing(contribution) or contribution <= 0:
        reasons.append("marge_contributive_non_positive")
    if _missing(row.get("max_cpa")) or row.get("max_cpa") <= 0:
        reasons.append("cpa_max_non_defini")
    deliverable = row.get("deliverable")
    reviewed = not _missing(deliverable)
    if (reviewed and not bool(deliverable)) or (not reviewed and row.get("delivery_effort") == "high"):
        reasons.append("offre_non_livrable")
    for flag in sorted(set(row.get("regulatory_flags") or []) & set(blocking_flags)):
        reasons.append(f"restriction_reglementaire:{flag}")
    return reasons


def readiness_issues(row: dict, *, test_budget: float, max_cpc: float, min_max_cpa: float) -> list[str]:
    """Critères 12.1 : CPC < 2 EUR, CPA max >= 30 EUR et budget >= 3 x CPA max."""
    issues = []
    cpc_eur, max_cpa = row.get("cpc_eur"), row.get("max_cpa")
    if _missing(cpc_eur) or cpc_eur >= max_cpc:
        issues.append(f"cpc_superieur_a_{max_cpc:g}_eur")
    if _missing(max_cpa) or max_cpa < min_max_cpa:
        issues.append(f"cpa_max_inferieur_a_{min_max_cpa:g}_eur")
    elif test_budget < 3 * max_cpa:
        issues.append("budget_test_inferieur_a_3x_cpa_max")
    return issues


def _missing(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA


def _num(v, default):
    return default if _missing(v) else float(v)


# ==========================================================================
# 5. Préparation du lot et lignes BigQuery
# ==========================================================================
def prepare(df: pd.DataFrame, *, usd_to_eur: float) -> tuple[pd.DataFrame, dict[str, dict]]:
    """Applique les hypothèses (Gemini, revues manuelles, défauts), simule les
    trois scénarios et renvoie (lot enrichi, scénarios par opportunity_id)."""
    records, sims = [], {}
    for r in df.to_dict("records"):
        motion = r.get("sales_motion") or "direct"
        cpc_usd = _num(r.get("weighted_cpc"), _num(r.get("avg_cpc"), float("nan")))
        price = _num(r.get("price_override_eur"), _num(r.get("price_hypothesis_eur"), float("nan")))
        margin = _num(r.get("margin_rate_override"), _num(r.get("margin_rate_hypothesis"), DEFAULT_MARGIN_RATE))
        l2s = _num(r.get("lead_to_sale_override"), _num(r.get("lead_to_sale_hypothesis"),
                                                         DEFAULT_LEAD_TO_SALE.get(motion, 1.0)))
        ltv_m = _num(r.get("ltv_months_override"), _num(r.get("ltv_months"), DEFAULT_LTV_MONTHS))
        r.update({
            "cpc_eur": None if math.isnan(cpc_usd) else round(cpc_usd * usd_to_eur, 4),
            "price_eur": None if math.isnan(price) else price,
            "margin_rate": min(max(margin, 0.0), 1.0),
            "lead_to_sale": min(max(l2s, 0.01), 1.0),
            "ltv_months": max(ltv_m, 1.0),
        })
        if r["cpc_eur"] is not None and r["price_eur"] is not None:
            sc = three_scenarios(r["cpc_eur"], r["price_eur"], r["margin_rate"], r["lead_to_sale"], r["ltv_months"])
            sims[r["opportunity_id"]] = sc
            c = sc["central"]
            r.update({
                "contribution": c["contribution"],
                "unit_contribution": round(c["contribution"] / r["ltv_months"], 2),
                "max_cpa": c["max_cpa"],
                "cpa_central": c["cpa"],
                "cpa_pessimistic": sc["pessimistic"]["cpa"],
                "profit_central": c["profit_per_sale"],
                "profit_pessimistic": sc["pessimistic"]["profit_per_sale"],
                "breakeven_cr": c["breakeven_cr"],
            })
        else:
            r.update({k: None for k in ("contribution", "unit_contribution", "max_cpa", "cpa_central",
                                        "cpa_pessimistic", "profit_central", "profit_pessimistic",
                                        "breakeven_cr")})
        records.append(r)
    return pd.DataFrame.from_records(records), sims


def build_rows(
    scored: pd.DataFrame,
    sims: dict[str, dict],
    *,
    run_id: str,
    computed_at: str,
    test_budget: float,
    max_cpc: float,
    min_max_cpa: float,
) -> tuple[list[dict], list[dict]]:
    scenario_rows, score_rows = [], []
    for r in scored.to_dict("records"):
        oid = r["opportunity_id"]
        for name, sc in sims.get(oid, {}).items():
            scenario_rows.append({
                "opportunity_id": oid, "run_id": run_id, "scenario": name,
                "cpc_eur": sc["cpc"], "cr": sc["cr"], "lead_to_sale": sc["lead_to_sale"],
                "refund_rate": sc["refund_rate"], "price_eur": sc["price"],
                "margin_rate": sc["margin_rate"], "ltv_months": sc["ltv_months"],
                "net_price_eur": sc["net_price"], "contribution": sc["contribution"],
                "cpa": sc["cpa"], "profit_per_sale": sc["profit_per_sale"], "roas": sc["roas"],
                "max_cpa": sc["max_cpa"], "breakeven_cr": sc["breakeven_cr"],
                **budget_outlook(test_budget, sc["cpc"], sc["cr"]),  # conversions = clic -> lead/achat
                "source": "bod.simulate", "data_kind": DATA_KIND_MODEL_ASSUMPTION,
                "computed_at": computed_at,
            })
        reasons = elimination_reasons(r)
        issues = readiness_issues(r, test_budget=test_budget, max_cpc=max_cpc, min_max_cpa=min_max_cpa)
        score_rows.append({
            "opportunity_id": oid, "cluster_id": r.get("cluster_id"), "run_id": run_id,
            "country_code": r.get("country_code"),
            **{k: r.get(k) for k in [f"mos_{x}" for x in MOS_WEIGHTS] + [f"pps_{x}" for x in PPS_WEIGHTS]},
            "mos": r.get("mos"), "pps": r.get("pps"), "priority_score": r.get("priority_score"),
            "cpc_eur": r.get("cpc_eur"), "price_eur": r.get("price_eur"),
            "margin_rate": r.get("margin_rate"), "lead_to_sale": r.get("lead_to_sale"),
            "ltv_months": r.get("ltv_months"),
            "eliminated": bool(reasons), "elimination_reasons": reasons,
            "test_ready": not reasons and not issues, "test_readiness_issues": issues,
            "source": "bod.simulate", "data_kind": DATA_KIND_MODEL_ASSUMPTION,
            "computed_at": computed_at,
        })
    return scenario_rows, score_rows


# ==========================================================================
# 6. Orchestration BigQuery
# ==========================================================================
INPUT_SQL = """
WITH opp AS (
  SELECT * FROM bod.opportunities
  WHERE DATE(generated_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
    AND country_code IN UNNEST(@countries)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY generated_at DESC) = 1
),
rev AS (
  SELECT * FROM bod.opportunity_reviews
  WHERE TRUE
  QUALIFY ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY reviewed_at DESC) = 1
)
SELECT
  o.opportunity_id, o.cluster_id, o.country_code, o.sales_motion, o.business_model,
  o.price_hypothesis_eur, o.margin_rate_hypothesis, o.lead_to_sale_hypothesis, o.ltv_months,
  o.delivery_effort, o.regulatory_flags, o.confidence,
  c.total_volume, c.avg_cpc, c.weighted_cpc, c.avg_competition, c.trend_yoy,
  c.commercial_share, c.n_keywords,
  r.deliverable, r.price_override_eur, r.margin_rate_override,
  r.lead_to_sale_override, r.ltv_months_override
FROM opp o
JOIN bod.clusters c USING (cluster_id)
LEFT JOIN rev r USING (opportunity_id)
"""


def run(markets: list[str] | None = None, run_id: str | None = None) -> int:
    from app.bq import load_rows, query, utcnow
    from app.config import get_settings

    s = get_settings()
    run_id = run_id or uuid.uuid4().hex[:8]
    df = query(INPUT_SQL, {"days": s.opportunity_lookback_days,
                           "countries": list(markets or s.active_markets)})
    if df.empty:
        log.info("simulate.nothing_to_do")
        return 0
    df["regulatory_flags"] = df["regulatory_flags"].map(lambda v: list(v) if v is not None else [])
    df["deliverable"] = df["deliverable"].astype(object).where(df["deliverable"].notna(), None)

    prepared, sims = prepare(df, usd_to_eur=s.usd_to_eur)
    scored = compute_scores(prepared)
    scenario_rows, score_rows = build_rows(
        scored, sims, run_id=run_id, computed_at=utcnow().isoformat(),
        test_budget=s.test_budget_eur, max_cpc=s.max_test_cpc_eur, min_max_cpa=s.min_test_max_cpa_eur,
    )
    load_rows("scenarios", scenario_rows)
    load_rows("opportunity_scores", score_rows)
    log.info("simulate.done", extra={
        "opportunities": len(score_rows),
        "eliminated": sum(r["eliminated"] for r in score_rows),
        "test_ready": sum(r["test_ready"] for r in score_rows),
    })
    return len(score_rows)

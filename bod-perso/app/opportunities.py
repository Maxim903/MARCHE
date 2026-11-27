"""Étape `opportunities` : du besoin à l'offre, via Gemini (Vertex AI, SDK google-genai).

Pour chaque cluster récent pas encore traité, Gemini produit une sortie JSON
STRUCTURÉE (schéma imposé) : besoin, segment, intention et 3 offres avec
modèle économique et HYPOTHÈSES de prix / marge. Rien n'est un fait :
toutes les lignes sont marquées data_kind = model_assumption.

Les réponses brutes du modèle sont archivées dans Cloud Storage.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from enum import Enum

from pydantic import BaseModel, ValidationError

from app.config import DATA_KIND_MODEL_ASSUMPTION, MARKETS, get_settings

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1"


# --------------------------------------------------------------------------
# Schéma de sortie imposé à Gemini
# --------------------------------------------------------------------------
class Level(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class IntentType(str, Enum):
    informational = "informational"
    commercial = "commercial"
    transactional = "transactional"
    navigational = "navigational"


class BusinessModel(str, Enum):
    one_time_product = "one_time_product"
    service = "service"
    subscription = "subscription"
    lead_generation = "lead_generation"
    digital_product = "digital_product"
    marketplace = "marketplace"


class SalesMotion(str, Enum):
    direct = "direct"  # achat direct en ligne
    lead = "lead"      # demande de devis / contact puis vente


class RegulatoryFlag(str, Enum):
    health = "health"
    finance = "finance"
    gambling = "gambling"
    alcohol = "alcohol"
    tobacco = "tobacco"
    weapons = "weapons"
    drugs = "drugs"
    adult = "adult"
    legal = "legal"
    political = "political"
    other = "other"


class Offer(BaseModel):
    name: str
    business_model: BusinessModel
    sales_motion: SalesMotion
    price_hypothesis_eur: float
    margin_rate_hypothesis: float
    lead_to_sale_hypothesis: float
    ltv_months: float
    delivery_effort: Level


class OpportunityLLM(BaseModel):
    need: str
    segment: str
    intent_type: IntentType
    offers: list[Offer]
    risks: list[str]
    regulatory_flags: list[RegulatoryFlag]
    confidence: Level


PROMPT = """Tu es un analyste d'opportunités commerciales.
Voici un groupe de recherches Google ({country}, langue {lang}) :
{keywords}
Volume mensuel cumulé : {volume}. CPC moyen : {cpc} USD. Concurrence publicitaire moyenne (0-1) : {competition}.

Réponds UNIQUEMENT en JSON avec les champs :
need (le besoin réel), segment (clientèle), intent_type,
offers (liste de 3 offres : name, business_model, sales_motion (direct = achat en ligne, lead = devis/contact),
price_hypothesis_eur (prix mensuel si abonnement), margin_rate_hypothesis (0-1), lead_to_sale_hypothesis (1 si achat direct, sinon 0.05-0.5),
ltv_months (1 si achat unique, sinon durée de vie moyenne d'un abonné en mois), delivery_effort (low|medium|high)
pour un solo-entrepreneur),
risks (liste), regulatory_flags (liste parmi health, finance, gambling, alcohol, tobacco, weapons, drugs,
adult, legal, political, other ; vide si aucun), confidence (low|medium|high).
Ne présente aucun prix comme un fait : ce sont des hypothèses à vérifier."""

CLUSTERS_SQL = """
WITH last_runs AS (
  SELECT country_code, MAX_BY(run_id, created_at) AS run_id
  FROM bod.clusters
  WHERE country_code IN UNNEST(@countries)
  GROUP BY country_code
)
SELECT c.*
FROM bod.clusters c
JOIN last_runs USING (country_code, run_id)
WHERE c.cluster_id NOT IN (SELECT DISTINCT cluster_id FROM bod.opportunities)
ORDER BY c.total_volume DESC
LIMIT @limit
"""


def sanitize(result: OpportunityLLM) -> OpportunityLLM:
    """Bornes métier appliquées après génération (le schéma Gemini reste simple).

    Offres sans prix positif écartées ; ratios bornés ; 3 offres maximum.
    """
    offers = []
    for o in result.offers:
        if not o.price_hypothesis_eur or o.price_hypothesis_eur <= 0:
            continue
        o.margin_rate_hypothesis = min(max(o.margin_rate_hypothesis, 0.0), 1.0)
        o.lead_to_sale_hypothesis = 1.0 if o.sales_motion == SalesMotion.direct else min(
            max(o.lead_to_sale_hypothesis, 0.01), 1.0)
        o.ltv_months = 1.0 if o.business_model != BusinessModel.subscription else min(
            max(o.ltv_months, 1.0), 60.0)
        offers.append(o)
    if not offers:
        raise ValueError("Aucune offre exploitable (prix manquant ou nul)")
    result.offers = offers[:3]
    return result


# --------------------------------------------------------------------------
# Construction (pur)
# --------------------------------------------------------------------------
def build_prompt(cluster: dict) -> str:
    country = cluster["country_code"]
    lang = cluster.get("language_code") or MARKETS[country][1]
    comp = cluster.get("avg_competition")
    return PROMPT.format(
        country=country,
        lang=lang,
        keywords="; ".join(list(cluster["top_keywords"])),
        volume=int(cluster["total_volume"]),
        cpc=round(float(cluster.get("weighted_cpc") or cluster["avg_cpc"]), 2),
        competition="inconnue" if comp is None else round(float(comp), 2),
    )


def opportunity_id(cluster_id: str, offer_name: str) -> str:
    h = hashlib.sha1(f"{cluster_id}|{offer_name.strip().lower()}".encode()).hexdigest()[:10]
    return f"OPP-{h.upper()}"


def to_rows(cluster: dict, result: OpportunityLLM, *, run_id: str, model: str, generated_at: str) -> list[dict]:
    rows = []
    for offer in result.offers:
        rows.append({
            "opportunity_id": opportunity_id(cluster["cluster_id"], offer.name),
            "cluster_id": cluster["cluster_id"],
            "run_id": run_id,
            "country_code": cluster["country_code"],
            "need": result.need,
            "segment": result.segment,
            "intent_type": result.intent_type.value,
            "offer_name": offer.name,
            "business_model": offer.business_model.value,
            "sales_motion": offer.sales_motion.value,
            "price_hypothesis_eur": offer.price_hypothesis_eur,
            "margin_rate_hypothesis": offer.margin_rate_hypothesis,
            "lead_to_sale_hypothesis": offer.lead_to_sale_hypothesis,
            "ltv_months": offer.ltv_months,
            "delivery_effort": offer.delivery_effort.value,
            "risks": list(result.risks),
            "regulatory_flags": sorted({f.value for f in result.regulatory_flags}),
            "confidence": result.confidence.value,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "source": "gemini",
            "data_kind": DATA_KIND_MODEL_ASSUMPTION,
            "generated_at": generated_at,
        })
    return rows


# --------------------------------------------------------------------------
# Appel Gemini
# --------------------------------------------------------------------------
def _client():
    from google import genai

    s = get_settings()
    return genai.Client(vertexai=True, project=s.require_project(), location=s.vertex_location)


def build_opportunity(client, cluster: dict, model: str) -> tuple[OpportunityLLM, str]:
    from google.genai import types

    r = client.models.generate_content(
        model=model,
        contents=build_prompt(cluster),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=OpportunityLLM,
            temperature=0.3,
        ),
    )
    parsed = getattr(r, "parsed", None)
    if not isinstance(parsed, OpportunityLLM):
        parsed = OpportunityLLM.model_validate_json(r.text)
    return sanitize(parsed), r.text


def run(markets: list[str] | None = None, run_id: str | None = None) -> int:
    from app.bq import load_rows, query, upload_json, utcnow

    s = get_settings()
    run_id = run_id or uuid.uuid4().hex[:8]
    clusters = query(
        CLUSTERS_SQL,
        {"countries": list(markets or s.active_markets), "limit": s.max_clusters_for_llm},
    )
    if clusters.empty:
        log.info("opportunities.nothing_to_do")
        return 0

    client = _client()
    rows: list[dict] = []
    raw_log: list[dict] = []
    failures = 0
    for cluster in clusters.to_dict("records"):
        try:
            result, raw_text = build_opportunity(client, cluster, s.gemini_model)
        except (ValidationError, ValueError) as exc:
            failures += 1
            log.warning("opportunities.invalid_output", extra={"cluster_id": cluster["cluster_id"], "error": str(exc)[:300]})
            continue
        except Exception as exc:  # quota, réseau… on n'arrête pas tout le lot
            failures += 1
            log.warning("opportunities.call_failed", extra={"cluster_id": cluster["cluster_id"], "error": str(exc)[:300]})
            continue
        now = utcnow().isoformat()
        rows += to_rows(cluster, result, run_id=run_id, model=s.gemini_model, generated_at=now)
        raw_log.append({"cluster_id": cluster["cluster_id"], "model": s.gemini_model,
                        "prompt_version": PROMPT_VERSION, "response": raw_text})

    if raw_log:
        upload_json(f"raw/gemini.opportunities/{utcnow().date().isoformat()}/{run_id}.json", raw_log,
                    {"run_id": run_id, "model": s.gemini_model})
    n = load_rows("opportunities", rows)
    log.info("opportunities.done", extra={"clusters": len(clusters), "rows": n, "failures": failures})
    return n

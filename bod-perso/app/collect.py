"""Étape `collect` : découverte de mots-clés via DataForSEO Labs.

Pour chaque marché actif et ses termes de départ :
  1. appel `keyword_ideas/live` (et optionnellement suggestions / related),
  2. réponse brute écrite dans Cloud Storage par le client,
  3. lignes normalisées chargées dans `bod.keywords_raw` (data_kind = api_estimate),
  4. optionnel : intention manquante complétée par `search_intent/live`.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from app.bq import load_rows, utcnow
from app.config import (
    DATA_KIND_API_ESTIMATE,
    EP_KEYWORD_IDEAS,
    EP_KEYWORD_SUGGESTIONS,
    EP_RELATED_KEYWORDS,
    EP_SEARCH_INTENT,
    MARKETS,
    get_settings,
)
from app.dataforseo_client import ApiResult, DataForSEO

log = logging.getLogger(__name__)

ENDPOINTS = {
    "keyword_ideas": EP_KEYWORD_IDEAS,
    "keyword_suggestions": EP_KEYWORD_SUGGESTIONS,
    "related_keywords": EP_RELATED_KEYWORDS,
}
MAX_SEEDS_PER_TASK = 200  # limite keyword_ideas (à vérifier dans la doc)
MAX_INTENT_KEYWORDS_PER_TASK = 1000


# --------------------------------------------------------------------------
# Parsing (pur, testable)
# --------------------------------------------------------------------------
def compute_yoy_trend(keyword_info: dict) -> float | None:
    """Croissance annuelle : tendance fournie, sinon 3 derniers mois vs N-1."""
    trend = (keyword_info.get("search_volume_trend") or {}).get("yearly")
    if trend is not None:
        return float(trend) / 100.0
    monthly = keyword_info.get("monthly_searches") or []
    vols = {
        (m.get("year"), m.get("month")): m.get("search_volume")
        for m in monthly
        if m.get("year") and m.get("month") and m.get("search_volume") is not None
    }
    if not vols:
        return None
    recent = sorted(vols)[-3:]
    current = sum(vols[k] for k in recent)
    previous_keys = [(y - 1, m) for (y, m) in recent]
    if not all(k in vols for k in previous_keys):
        return None
    previous = sum(vols[k] for k in previous_keys)
    return None if previous == 0 else current / previous - 1.0


def _item_payload(item: dict) -> tuple[str | None, dict, dict]:
    """Uniformise les formats `keyword_ideas`/`suggestions` et `related_keywords`."""
    if "keyword_data" in item:  # related_keywords
        kd = item.get("keyword_data") or {}
        return kd.get("keyword"), kd.get("keyword_info") or {}, kd.get("search_intent_info") or {}
    return item.get("keyword"), item.get("keyword_info") or {}, item.get("search_intent_info") or {}


def parse_keyword_items(
    result: ApiResult | dict,
    *,
    country: str,
    endpoint_name: str,
    run_id: str,
    seed: str | None = None,
    observed_at: str | None = None,
) -> list[dict]:
    """Transforme une réponse DataForSEO Labs en lignes `bod.keywords_raw`."""
    if isinstance(result, ApiResult):
        data, batch_id, raw_uri = result.data, result.batch_id, result.raw_uri
    else:
        data, batch_id, raw_uri = result, result.get("_batch_id"), None
    loc, lang = MARKETS[country]
    observed_at = observed_at or utcnow().isoformat()
    rows: dict[str, dict] = {}
    for task in data.get("tasks") or []:
        for res in task.get("result") or []:
            for item in res.get("items") or []:
                kw, ki, ii = _item_payload(item)
                if not kw:
                    continue
                key = kw.strip().lower()
                if key in rows:  # dédoublonnage dans le lot
                    continue
                rows[key] = {
                    "keyword": key,
                    "country_code": country,
                    "location_code": loc,
                    "language_code": lang,
                    "search_volume": ki.get("search_volume"),
                    "cpc": ki.get("cpc"),  # USD, donnée fournisseur
                    "competition": ki.get("competition"),
                    "competition_level": ki.get("competition_level"),
                    "low_top_of_page_bid": ki.get("low_top_of_page_bid"),
                    "high_top_of_page_bid": ki.get("high_top_of_page_bid"),
                    "trend_yoy": compute_yoy_trend(ki),
                    "intent": ii.get("main_intent"),
                    "seed": seed,
                    "source": "dataforseo",
                    "endpoint": f"labs/{endpoint_name}",
                    "data_kind": DATA_KIND_API_ESTIMATE,
                    "batch_id": batch_id,
                    "run_id": run_id,
                    "raw_uri": raw_uri,
                    "observed_at": observed_at,
                }
    return list(rows.values())


def parse_intent_items(result: ApiResult | dict) -> dict[str, str]:
    data = result.data if isinstance(result, ApiResult) else result
    out: dict[str, str] = {}
    for task in data.get("tasks") or []:
        for res in task.get("result") or []:
            for item in res.get("items") or []:
                kw = (item.get("keyword") or "").strip().lower()
                intent = (item.get("keyword_intent") or {}).get("label")
                if kw and intent:
                    out[kw] = intent
    return out


def _chunks(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


# --------------------------------------------------------------------------
# Appels API
# --------------------------------------------------------------------------
def _tasks_for(endpoint_name: str, seeds: list[str], loc: int, lang: str, limit: int, min_vol: int) -> list[tuple[str | None, list[dict]]]:
    flt = ["keyword_info.search_volume", ">", min_vol]
    if endpoint_name == "keyword_ideas":
        return [
            (None, [{
                "keywords": chunk, "location_code": loc, "language_code": lang,
                "limit": limit, "filters": flt,
            }])
            for chunk in _chunks(seeds, MAX_SEEDS_PER_TASK)
        ]
    if endpoint_name == "keyword_suggestions":
        return [(s, [{"keyword": s, "location_code": loc, "language_code": lang,
                      "limit": limit, "filters": flt}]) for s in seeds]
    if endpoint_name == "related_keywords":
        return [(s, [{"keyword": s, "location_code": loc, "language_code": lang,
                      "depth": 2, "limit": limit,
                      "filters": ["keyword_data.keyword_info.search_volume", ">", min_vol]}])
                for s in seeds]
    raise ValueError(f"Endpoint de collecte inconnu : {endpoint_name}")


def fill_missing_intent(api: DataForSEO, rows: list[dict], country: str) -> int:
    loc, lang = MARKETS[country]
    missing = [r["keyword"] for r in rows if not r.get("intent")]
    filled = 0
    for chunk in _chunks(missing, MAX_INTENT_KEYWORDS_PER_TASK):
        res = api.post(EP_SEARCH_INTENT, [{"keywords": chunk, "language_code": lang}], country)
        intents = parse_intent_items(res)
        for r in rows:
            if not r.get("intent") and r["keyword"] in intents:
                r["intent"] = intents[r["keyword"]]
                filled += 1
    return filled


def collect_market(api: DataForSEO, country: str, seeds: list[str]) -> int:
    s = get_settings()
    if country not in MARKETS:
        raise ValueError(f"Marché inconnu : {country} (voir config.MARKETS)")
    loc, lang = MARKETS[country]
    seen: set[str] = set()
    n = 0
    for endpoint_name in s.collect_endpoints:
        path = ENDPOINTS[endpoint_name]
        for seed, tasks in _tasks_for(endpoint_name, seeds, loc, lang, s.collect_limit, s.min_search_volume):
            res = api.post(path, tasks, country)
            rows = [
                r for r in parse_keyword_items(
                    res, country=country, endpoint_name=endpoint_name, run_id=api.run_id, seed=seed
                )
                if r["keyword"] not in seen
            ]
            seen.update(r["keyword"] for r in rows)
            if s.fill_missing_intent and rows and not api.exhausted:
                log.info("collect.intent", extra={"filled": fill_missing_intent(api, rows, country)})
            # Chargement après chaque appel : rien n'est perdu si le plafond coupe la suite.
            n += load_rows("keywords_raw", rows)
    log.info(
        "collect.market_done",
        extra={"country": country, "keywords": n, "spent_usd": round(api.spent, 4)},
    )
    return n


def run(api: DataForSEO, markets: list[str] | None = None) -> dict[str, int]:
    """Collecte tous les marchés actifs. BudgetExceeded remonte à l'orchestrateur
    (les lignes déjà chargées restent en base)."""
    s = get_settings()
    seeds = s.seeds()
    out: dict[str, int] = {}
    for country in markets or s.active_markets:
        if not seeds.get(country):
            log.warning("collect.no_seeds", extra={"country": country})
            continue
        out[country] = collect_market(api, country, seeds[country])
    return out

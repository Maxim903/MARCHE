"""Étape `serp` : analyse profonde des SERP, RÉSERVÉE aux meilleures opportunités.

Jamais lancée sur l'ensemble des mots-clés bruts : on prend le top N (~10)
de `bod.v_opportunities_ranked` (opportunités non éliminées), puis les K
mots-clés les plus volumineux de chaque cluster (~3). Soit ~30 SERP au maximum,
avec un plafond dur `SERP_MAX_CALLS` en plus du plafond de coût du client.

Indicateurs : nombre d'annonces, marketplaces dans le top 10, part de résultats
informationnels, prix visibles dans les titres/extraits, People Also Ask.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from app.config import (
    DATA_KIND_API_ESTIMATE,
    EP_SERP_ORGANIC,
    EP_SERP_SHOPPING,
    MARKETS,
    get_settings,
)
from app.dataforseo_client import ApiResult, DataForSEO

log = logging.getLogger(__name__)

MARKETPLACES = (
    "amazon.", "ebay.", "etsy.", "cdiscount.", "leboncoin.", "aliexpress.", "walmart.",
    "fnac.", "rakuten.", "manomano.", "temu.", "zalando.", "otto.de", "flipkart.",
)
INFORMATIONAL_DOMAINS = ("wikipedia.", "youtube.", "reddit.", "quora.", "pinterest.", "commentcamarche.")
INFORMATIONAL_TITLE = re.compile(
    r"\b(how|what|why|guide|tutorial|comment|pourquoi|qu'est|quest-ce|tuto|wie|was ist|anleitung)\b",
    re.IGNORECASE,
)
PRICE_RE = re.compile(
    r"(?:[€$£]\s?(\d{1,5}(?:[.,]\d{1,2})?))|(?:(\d{1,5}(?:[.,]\d{1,2})?)\s?(?:€|eur\b|euros?\b|\$|usd\b|£))",
    re.IGNORECASE,
)

TOP_SQL = """
SELECT opportunity_id, cluster_id, country_code, priority_score
FROM bod.v_opportunities_ranked
WHERE country_code IN UNNEST(@countries)
QUALIFY ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY priority_score DESC) = 1
ORDER BY priority_score DESC
LIMIT @top_n
"""

KEYWORDS_SQL = """
SELECT cluster_id, keyword
FROM bod.cluster_keywords
WHERE cluster_id IN UNNEST(@clusters)
QUALIFY ROW_NUMBER() OVER (PARTITION BY cluster_id ORDER BY search_volume DESC) <= @k
"""

RECENT_SQL = """
SELECT DISTINCT country_code, keyword
FROM bod.serp_snapshots
WHERE DATE(observed_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
"""


# --------------------------------------------------------------------------
# Parsing (pur, testable)
# --------------------------------------------------------------------------
def _domain(it: dict) -> str:
    d = it.get("domain") or urlparse(it.get("url") or "").netloc
    return (d or "").lower().removeprefix("www.")


def extract_prices(text: str) -> list[float]:
    out = []
    for a, b in PRICE_RE.findall(text or ""):
        try:
            out.append(float((a or b).replace(",", ".")))
        except ValueError:
            continue
    return out


def parse_serp(data: dict) -> dict:
    """Résumé d'une réponse `serp/google/organic/live/advanced`."""
    items: list[dict] = []
    for task in data.get("tasks") or []:
        for res in task.get("result") or []:
            items.extend(res.get("items") or [])

    paid, organic, paa, item_types = [], [], 0, set()
    for it in items:
        t = it.get("type")
        item_types.add(t)
        if t == "paid":
            paid.append({"domain": _domain(it), "title": it.get("title"), "description": it.get("description")})
        elif t == "organic":
            organic.append({"domain": _domain(it), "url": it.get("url"), "title": it.get("title"),
                            "description": it.get("description"), "rank": it.get("rank_group")})
        elif t == "people_also_ask":
            paa += len(it.get("items") or [])

    top10 = sorted(organic, key=lambda o: o.get("rank") or 999)[:10]
    marketplaces = sorted({o["domain"] for o in top10 if any(m in o["domain"] for m in MARKETPLACES)})
    informational = [
        o for o in top10
        if any(d in o["domain"] for d in INFORMATIONAL_DOMAINS) or INFORMATIONAL_TITLE.search(o.get("title") or "")
    ]
    prices = []
    for o in paid + top10:
        prices += extract_prices(f"{o.get('title') or ''} {o.get('description') or ''}")

    return {
        "n_paid": len(paid),
        "n_organic": len(organic),
        "paa_count": paa,
        "has_shopping": bool(item_types & {"shopping", "popular_products", "commercial_units"}),
        "marketplaces_top10": marketplaces,
        "marketplace_count": len(marketplaces),
        "informational_share": round(len(informational) / len(top10), 3) if top10 else None,
        "price_mentions": prices[:20],
        "price_median": sorted(prices)[len(prices) // 2] if prices else None,
        "top_domains": [o["domain"] for o in top10],
        "paid": paid,
        "organic": top10,
    }


# --------------------------------------------------------------------------
# Appels API
# --------------------------------------------------------------------------
def serp_snapshot(api: DataForSEO, keyword: str, country: str) -> tuple[dict, ApiResult]:
    s = get_settings()
    loc, lang = MARKETS[country]
    tasks = [{"keyword": keyword, "location_code": loc, "language_code": lang,
              "device": s.serp_device, "depth": s.serp_depth}]
    res = api.post(EP_SERP_ORGANIC, tasks, country)
    return parse_serp(res.data), res


def shopping_prices(api: DataForSEO, keyword: str, country: str) -> list[float]:
    loc, lang = MARKETS[country]
    res = api.post(EP_SERP_SHOPPING, [{"keyword": keyword, "location_code": loc, "language_code": lang}], country)
    prices = []
    for r in res.results():
        for it in r.get("items") or []:
            if isinstance(it.get("price"), (int, float)):
                prices.append(float(it["price"]))
    return prices


def run(api: DataForSEO, markets: list[str] | None = None) -> int:
    from app.bq import load_rows, query, utcnow

    s = get_settings()
    top = query(TOP_SQL, {"countries": list(markets or s.active_markets), "top_n": s.serp_top_n})
    if top.empty:
        log.info("serp.nothing_to_do")
        return 0
    kws = query(KEYWORDS_SQL, {"clusters": top["cluster_id"].tolist(), "k": s.serp_keywords_per_opportunity})
    recent = query(RECENT_SQL, {"days": s.serp_refresh_days})
    already = set(zip(recent["country_code"], recent["keyword"])) if not recent.empty else set()

    calls, written = 0, 0
    for opp in top.to_dict("records"):
        country = opp["country_code"]
        for kw in kws[kws.cluster_id == opp["cluster_id"]]["keyword"].tolist():
            if (country, kw) in already:
                continue
            if calls >= s.serp_max_calls:
                log.warning("serp.max_calls_reached", extra={"calls": calls})
                return written
            summary, res = serp_snapshot(api, kw, country)
            calls += 1
            if s.serp_shopping and not api.exhausted:
                shop = shopping_prices(api, kw, country)
                summary["price_mentions"] = (summary["price_mentions"] + shop)[:40]
            row = {
                "opportunity_id": opp["opportunity_id"],
                "cluster_id": opp["cluster_id"],
                "keyword": kw,
                "country_code": country,
                **{k: v for k, v in summary.items() if k not in ("paid", "organic")},
                "paid_json": json.dumps(summary["paid"], ensure_ascii=False),
                "organic_json": json.dumps(summary["organic"], ensure_ascii=False),
                "run_id": api.run_id,
                "batch_id": res.batch_id,
                "raw_uri": res.raw_uri,
                "cost_usd": res.cost_usd,
                "source": "dataforseo",
                "endpoint": EP_SERP_ORGANIC,
                "data_kind": DATA_KIND_API_ESTIMATE,
                "observed_at": utcnow().isoformat(),
            }
            written += load_rows("serp_snapshots", [row])
    log.info("serp.done", extra={"calls": calls, "rows": written, "spent_usd": round(api.spent, 4)})
    return written

"""Étape `cluster` : des requêtes aux besoins.

1. Lecture de la dernière observation de chaque mot-clé (`bod.keywords_raw`).
2. Filtrage / normalisation (section 9.1) : minuscules, doublons, marques et
   navigation exclues, intentions commerciales/transactionnelles, volume et CPC > 0.
3. Embeddings multilingues Vertex AI (SDK google-genai, task_type CLUSTERING).
4. HDBSCAN sur vecteurs normalisés (euclidienne ~ cosinus), bruit (-1) ignoré.
5. Écriture `bod.clusters` et `bod.cluster_keywords`.

Un pays + une langue à la fois.
"""

from __future__ import annotations

import logging
import re
import uuid

import numpy as np
import pandas as pd

from app.config import COMMERCIAL_INTENTS, DATA_KIND_API_ESTIMATE, MARKETS, get_settings

log = logging.getLogger(__name__)

KEYWORDS_SQL = """
SELECT * EXCEPT(rn) FROM (
  SELECT k.*,
         ROW_NUMBER() OVER (PARTITION BY country_code, LOWER(TRIM(keyword))
                            ORDER BY observed_at DESC) AS rn
  FROM bod.keywords_raw k
  WHERE DATE(observed_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL @days DAY)
    AND country_code = @country
)
WHERE rn = 1
"""


# --------------------------------------------------------------------------
# Filtrage (pur)
# --------------------------------------------------------------------------
def normalize_keyword(kw: str) -> str:
    return re.sub(r"\s+", " ", str(kw).strip().lower())


def filter_keywords(
    df: pd.DataFrame,
    *,
    exclude_terms: list[str],
    include_informational: bool = False,
    keep_unknown_intent: bool = True,
) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    out["keyword"] = out["keyword"].map(normalize_keyword)
    out = out.drop_duplicates(subset=["keyword"])
    # Sans volume ou sans CPC observé : pas d'économie publicitaire.
    out = out[(out["search_volume"].fillna(0) > 0) & (out["cpc"].fillna(0) > 0)]
    if exclude_terms:
        pattern = r"\b(?:" + "|".join(re.escape(t.lower()) for t in exclude_terms) + r")\b"
        out = out[~out["keyword"].str.contains(pattern, regex=True)]
    keep = set(COMMERCIAL_INTENTS) | ({"informational"} if include_informational else set())
    intent = out["intent"].fillna("").str.lower()
    # Intention absente (non renvoyée par l'API) : conservée par défaut, sinon tout
    # un lot peut disparaître. Les intentions informationnelles/navigationnelles
    # explicites restent exclues.
    out = out[intent.isin(keep) | ((intent == "") & keep_unknown_intent)]
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------
def embed(texts: list[str], batch: int | None = None) -> np.ndarray:
    from google import genai
    from google.genai import types

    s = get_settings()
    client = genai.Client(vertexai=True, project=s.require_project(), location=s.vertex_location)
    batch = batch or s.embedding_batch_size
    vecs: list[list[float]] = []
    for i in range(0, len(texts), batch):
        resp = client.models.embed_content(
            model=s.embedding_model,
            contents=texts[i : i + batch],
            config=types.EmbedContentConfig(task_type="CLUSTERING"),
        )
        vecs += [e.values for e in resp.embeddings]
    return np.array(vecs, dtype=np.float32)


# --------------------------------------------------------------------------
# Clustering (pur, à embeddings donnés)
# --------------------------------------------------------------------------
def cluster_keywords(
    df: pd.DataFrame, vectors: np.ndarray, min_cluster_size: int = 8
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Renvoie (mots-clés avec label, agrégats par cluster). Bruit exclu."""
    from sklearn.cluster import HDBSCAN
    from sklearn.preprocessing import normalize

    X = normalize(vectors)  # euclidienne sur vecteurs normalisés ~ cosinus
    model = HDBSCAN(min_cluster_size=min_cluster_size, copy=True).fit(X)
    df = df.assign(cluster=model.labels_, membership=model.probabilities_)
    df = df[df.cluster >= 0].copy()
    if df.empty:
        return df, pd.DataFrame()

    def _agg(g: pd.DataFrame) -> pd.Series:
        g = g.sort_values("search_volume", ascending=False)
        vol = g["search_volume"].fillna(0).astype(float)
        total = float(vol.sum())
        w = vol / total if total else pd.Series(1.0 / len(g), index=g.index)
        intents = g["intent"].fillna("unknown").str.lower()
        trend = g["trend_yoy"].astype(float) if "trend_yoy" in g else pd.Series(dtype=float)
        trend_mask = trend.notna() if len(trend) else pd.Series(dtype=bool)
        return pd.Series({
            "label": g["keyword"].iloc[0],
            "n_keywords": int(len(g)),
            "total_volume": int(total),
            "avg_cpc": float(g["cpc"].mean()),
            "weighted_cpc": float((g["cpc"].fillna(0) * w).sum()),
            "avg_competition": float(g["competition"].astype(float).mean()) if g["competition"].notna().any() else None,
            "trend_yoy": float((trend[trend_mask] * vol[trend_mask]).sum() / vol[trend_mask].sum())
            if len(trend) and trend_mask.any() and vol[trend_mask].sum() > 0 else None,
            "dominant_intent": intents.mode().iloc[0],
            "commercial_share": float(intents.isin(COMMERCIAL_INTENTS).mean()),
            "top_keywords": list(g["keyword"].head(10)),
        })

    agg = df.groupby("cluster").apply(_agg, include_groups=False).reset_index()
    return df, agg


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def run_market(country: str, run_id: str) -> int:
    from app.bq import load_rows, query, utcnow

    s = get_settings()
    _, lang = MARKETS[country]
    raw = query(KEYWORDS_SQL, {"days": s.keyword_lookback_days, "country": country})
    df = filter_keywords(raw, exclude_terms=s.exclude_terms, include_informational=s.include_informational,
                         keep_unknown_intent=s.keep_unknown_intent)
    if len(df) > s.max_keywords_to_embed:  # garde-fou coût Vertex
        df = df.nlargest(s.max_keywords_to_embed, "search_volume")
    log.info("cluster.input", extra={"country": country, "raw": len(raw), "kept": len(df)})
    if len(df) < max(2, s.min_cluster_size):
        log.warning("cluster.not_enough_keywords", extra={"country": country, "kept": len(df)})
        return 0

    vectors = embed(df["keyword"].tolist())
    members, agg = cluster_keywords(df, vectors, s.min_cluster_size)
    if agg.empty:
        log.warning("cluster.no_cluster", extra={"country": country})
        return 0

    now = utcnow().isoformat()
    agg["cluster_id"] = agg["cluster"].map(lambda c: f"CL-{country}-{run_id}-{int(c):04d}")
    cluster_rows = [
        {
            **{k: r[k] for k in (
                "cluster_id", "label", "n_keywords", "total_volume", "avg_cpc", "weighted_cpc",
                "avg_competition", "trend_yoy", "dominant_intent", "commercial_share", "top_keywords",
            )},
            "run_id": run_id,
            "country_code": country,
            "language_code": lang,
            "method": "hdbscan",
            "min_cluster_size": s.min_cluster_size,
            "embedding_model": s.embedding_model,
            "source": "dataforseo+vertex_ai",
            "data_kind": DATA_KIND_API_ESTIMATE,  # agrégats de métriques fournisseur
            "created_at": now,
        }
        for r in agg.to_dict("records")
    ]
    id_map = dict(zip(agg["cluster"], agg["cluster_id"]))
    member_rows = [
        {
            "cluster_id": id_map[r["cluster"]],
            "run_id": run_id,
            "keyword": r["keyword"],
            "country_code": country,
            "search_volume": r.get("search_volume"),
            "cpc": r.get("cpc"),
            "competition": r.get("competition"),
            "intent": r.get("intent"),
            "membership": r.get("membership"),
            "data_kind": DATA_KIND_API_ESTIMATE,
            "created_at": now,
        }
        for r in members.to_dict("records")
    ]
    load_rows("clusters", cluster_rows)
    load_rows("cluster_keywords", member_rows)
    log.info(
        "cluster.done",
        extra={"country": country, "clusters": len(cluster_rows), "keywords": len(member_rows)},
    )
    return len(cluster_rows)


def run(markets: list[str] | None = None, run_id: str | None = None) -> dict[str, int]:
    s = get_settings()
    run_id = run_id or uuid.uuid4().hex[:8]
    return {c: run_market(c, run_id) for c in (markets or s.active_markets)}

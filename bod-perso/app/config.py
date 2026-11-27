"""Configuration centrale de BOD Perso.

Tout ce qui est susceptible de changer (projet GCP, régions, modèles, plafonds,
marchés, pondérations) est ici. Les valeurs sensibles (identifiants DataForSEO)
ne sont JAMAIS dans ce fichier : elles sont lues dans Secret Manager.

Le module n'exige aucune variable d'environnement à l'import : les tests
unitaires peuvent l'importer hors de Google Cloud. `get_settings()` valide
au moment de l'exécution.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return str(_env(name, str(default))).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if raw is None:
        return list(default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# --------------------------------------------------------------------------
# Nature des données (traçabilité stricte, colonne `data_kind`)
# --------------------------------------------------------------------------
DATA_KIND_API_ESTIMATE = "api_estimate"        # estimé par le fournisseur (DataForSEO, Google Ads)
DATA_KIND_MODEL_ASSUMPTION = "model_assumption"  # hypothèse du modèle (Gemini, simulateur, scores)
DATA_KIND_ACTUAL = "actual"                    # résultat réel de campagne
DATA_KINDS = (DATA_KIND_API_ESTIMATE, DATA_KIND_MODEL_ASSUMPTION, DATA_KIND_ACTUAL)

# --------------------------------------------------------------------------
# Marchés : country -> (location_code, language_code)
# Codes à confirmer via l'endpoint de liste des localisations DataForSEO.
# --------------------------------------------------------------------------
MARKETS: dict[str, tuple[int, str]] = {
    "FR": (2250, "fr"),
    "US": (2840, "en"),
    "GB": (2826, "en"),
    "IN": (2356, "en"),
    "DE": (2276, "de"),
}

# Termes de départ par défaut (section 8.4 du guide). Remplaçables par SEEDS_FILE.
DEFAULT_SEEDS: dict[str, list[str]] = {
    "FR": ["nettoyage canape", "logiciel facturation", "devis renovation salle de bain"],
    "US": ["sofa cleaning service", "invoice software", "bathroom remodel quote"],
}

# --------------------------------------------------------------------------
# DataForSEO
# --------------------------------------------------------------------------
DATAFORSEO_BASE_URL = "https://api.dataforseo.com/v3"
DFS_STATUS_OK = 20000

# Endpoints (préfixe /v3/ implicite). À valider dans la documentation officielle.
EP_KEYWORD_IDEAS = "dataforseo_labs/google/keyword_ideas/live"
EP_KEYWORD_SUGGESTIONS = "dataforseo_labs/google/keyword_suggestions/live"
EP_RELATED_KEYWORDS = "dataforseo_labs/google/related_keywords/live"
EP_SEARCH_INTENT = "dataforseo_labs/google/search_intent/live"
EP_HISTORICAL = "dataforseo_labs/google/historical_keyword_data/live"
EP_ADS_SEARCH_VOLUME = "keywords_data/google_ads/search_volume/live"
EP_SERP_ORGANIC = "serp/google/organic/live/advanced"
EP_SERP_SHOPPING = "serp/google/shopping/live/advanced"

# Estimation PRUDENTE (majorée) du coût d'un appel, utilisée uniquement comme
# garde-fou AVANT l'appel (on refuse un appel qui ferait dépasser le plafond).
# Le coût réel retenu est TOUJOURS le champ `cost` de la réponse.
# Tarifs à revérifier sur la page de tarification DataForSEO.
COST_ESTIMATES_USD: dict[str, float] = {
    EP_KEYWORD_IDEAS: 0.15,
    EP_KEYWORD_SUGGESTIONS: 0.15,
    EP_RELATED_KEYWORDS: 0.15,
    EP_SEARCH_INTENT: 0.05,
    EP_HISTORICAL: 0.15,
    EP_ADS_SEARCH_VOLUME: 0.10,
    EP_SERP_ORGANIC: 0.01,
    EP_SERP_SHOPPING: 0.01,
}

# --------------------------------------------------------------------------
# Filtrage / normalisation (section 9.1)
# --------------------------------------------------------------------------
COMMERCIAL_INTENTS = ("commercial", "transactional")
# Termes de navigation / marques à exclure (complétables via env EXCLUDE_TERMS).
DEFAULT_EXCLUDE_TERMS = [
    "login", "log in", "sign in", "connexion", "se connecter", "mon compte",
    "youtube", "facebook", "instagram", "tiktok", "amazon", "leboncoin",
    "ebay", "wikipedia", "gratuit", "free download", "pdf",
]

# --------------------------------------------------------------------------
# Restrictions réglementaires (éliminatoires) — valeurs autorisées pour Gemini
# --------------------------------------------------------------------------
REGULATORY_FLAGS = (
    "health", "finance", "gambling", "alcohol", "tobacco",
    "weapons", "drugs", "adult", "legal", "political", "other",
)
BLOCKING_REGULATORY_FLAGS = frozenset(
    {"health", "finance", "gambling", "alcohol", "tobacco", "weapons", "drugs", "adult", "political"}
)

# --------------------------------------------------------------------------
# Simulation (section 10.3) — ordres de grandeur, PAS des garanties.
# Remplacer par les mesures réelles dès le premier test.
# --------------------------------------------------------------------------
DEFAULT_SCENARIOS: dict[str, dict[str, float]] = {
    "pessimistic": {"cpc_multiplier": 1.3, "cr": 0.01, "refund_rate": 0.08},
    "central": {"cpc_multiplier": 1.0, "cr": 0.03, "refund_rate": 0.05},
    "optimistic": {"cpc_multiplier": 0.8, "cr": 0.05, "refund_rate": 0.03},
}

# Pondérations initiales (section 10.1) : outil de tri, pas modèle prédictif.
MOS_WEIGHTS: dict[str, float] = {
    "demand": 0.15,
    "intent": 0.20,
    "growth": 0.15,
    "gap": 0.15,
    "solvency": 0.15,
    "size": 0.10,
    "feasibility": 0.10,
}
PPS_WEIGHTS: dict[str, float] = {
    "margin": 0.30,
    "cpa_accessibility": 0.25,
    "ad_competitiveness": 0.20,
    "ltv": 0.15,
    "reliability": 0.10,
}

# Hypothèses par défaut si Gemini ne fournit pas la valeur (model_assumption).
DEFAULT_MARGIN_RATE = 0.5
DEFAULT_LEAD_TO_SALE = {"direct": 1.0, "lead": 0.2}
DEFAULT_LTV_MONTHS = 1.0


@dataclass(frozen=True)
class Settings:
    project_id: str | None
    region: str
    bq_location: str
    vertex_location: str
    dataset: str
    raw_bucket: str | None
    gemini_model: str
    embedding_model: str
    embedding_batch_size: int
    stage: str
    active_markets: list[str]
    seeds_file: str | None
    max_cost_usd: float
    dfs_login_secret: str
    dfs_password_secret: str
    dfs_timeout_s: int
    dfs_retries: int
    collect_endpoints: list[str]
    collect_limit: int
    min_search_volume: int
    fill_missing_intent: bool
    keyword_lookback_days: int
    include_informational: bool
    exclude_terms: list[str]
    min_cluster_size: int
    max_keywords_to_embed: int
    max_clusters_for_llm: int
    opportunity_lookback_days: int
    usd_to_eur: float
    test_budget_eur: float
    max_test_cpc_eur: float
    min_test_max_cpa_eur: float
    serp_top_n: int
    serp_keywords_per_opportunity: int
    serp_max_calls: int
    serp_depth: int
    serp_device: str
    serp_refresh_days: int
    serp_shopping: bool
    extra: dict = field(default_factory=dict)

    # ---- helpers -------------------------------------------------------
    def require_project(self) -> str:
        if not self.project_id:
            raise RuntimeError(
                "PROJECT_ID n'est pas défini. Exporte PROJECT_ID (voir .env.example)."
            )
        return self.project_id

    @property
    def bucket(self) -> str:
        return self.raw_bucket or f"{self.require_project()}-raw"

    def table(self, name: str) -> str:
        """Identifiant BigQuery complet `projet.dataset.table`."""
        return f"{self.require_project()}.{self.dataset}.{name}"

    def seeds(self) -> dict[str, list[str]]:
        if self.seeds_file:
            data = json.loads(Path(self.seeds_file).read_text(encoding="utf-8"))
            return {k.upper(): list(v) for k, v in data.items()}
        return {k: list(v) for k, v in DEFAULT_SEEDS.items()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        project_id=_env("PROJECT_ID") or _env("GOOGLE_CLOUD_PROJECT"),
        region=_env("REGION", "europe-west1"),
        bq_location=_env("BQ_LOCATION", "EU"),
        vertex_location=_env("VERTEX_LOCATION", "europe-west4"),
        dataset=_env("BQ_DATASET", "bod"),
        raw_bucket=_env("RAW_BUCKET"),
        # Noms de modèles volontairement configurables : à vérifier dans ta région.
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
        embedding_model=_env("EMBEDDING_MODEL", "text-multilingual-embedding-002"),
        embedding_batch_size=_env_int("EMBEDDING_BATCH_SIZE", 50),
        stage=_env("STAGE", "collect"),
        active_markets=[m.upper() for m in _env_list("MARKETS", ["FR", "US"])],
        seeds_file=_env("SEEDS_FILE"),
        max_cost_usd=_env_float("MAX_COST_USD", 5.0),
        dfs_login_secret=_env("DFS_LOGIN_SECRET", "dataforseo-login"),
        dfs_password_secret=_env("DFS_PASSWORD_SECRET", "dataforseo-password"),
        dfs_timeout_s=_env_int("DFS_TIMEOUT_S", 180),
        dfs_retries=_env_int("DFS_RETRIES", 3),
        collect_endpoints=_env_list("COLLECT_ENDPOINTS", ["keyword_ideas"]),
        collect_limit=_env_int("COLLECT_LIMIT", 700),
        min_search_volume=_env_int("MIN_SEARCH_VOLUME", 50),
        fill_missing_intent=_env_bool("FILL_MISSING_INTENT", False),
        keyword_lookback_days=_env_int("KEYWORD_LOOKBACK_DAYS", 35),
        include_informational=_env_bool("INCLUDE_INFORMATIONAL", False),
        exclude_terms=_env_list("EXCLUDE_TERMS", DEFAULT_EXCLUDE_TERMS),
        min_cluster_size=_env_int("MIN_CLUSTER_SIZE", 8),
        max_keywords_to_embed=_env_int("MAX_KEYWORDS_TO_EMBED", 20000),
        max_clusters_for_llm=_env_int("MAX_CLUSTERS_FOR_LLM", 300),
        opportunity_lookback_days=_env_int("OPPORTUNITY_LOOKBACK_DAYS", 30),
        usd_to_eur=_env_float("USD_TO_EUR", 0.92),
        test_budget_eur=_env_float("TEST_BUDGET_EUR", 180.0),
        max_test_cpc_eur=_env_float("MAX_TEST_CPC_EUR", 2.0),
        min_test_max_cpa_eur=_env_float("MIN_TEST_MAX_CPA_EUR", 30.0),
        serp_top_n=_env_int("SERP_TOP_N", 10),
        serp_keywords_per_opportunity=_env_int("SERP_KEYWORDS_PER_OPPORTUNITY", 3),
        serp_max_calls=_env_int("SERP_MAX_CALLS", 30),
        serp_depth=_env_int("SERP_DEPTH", 20),
        serp_device=_env("SERP_DEVICE", "desktop"),
        serp_refresh_days=_env_int("SERP_REFRESH_DAYS", 7),
        serp_shopping=_env_bool("SERP_SHOPPING", False),
    )

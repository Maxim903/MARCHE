-- ============================================================================
-- BOD Perso - schéma BigQuery (dataset `bod`)
--
-- Traçabilité : chaque table porte `source`, une date d'observation/calcul et
-- `data_kind` IN ('api_estimate', 'model_assumption', 'actual') afin de ne jamais
-- mélanger estimations fournisseur, hypothèses de simulation et résultats réels.
--
-- Application : `python -m app.main --stage init` (qualifie `bod.` avec le projet
-- et le dataset configurés), ou `bq query --use_legacy_sql=false < sql/schema.sql`
-- après `bq mk -d bod` (projet par défaut de gcloud).
-- Idempotent : CREATE TABLE IF NOT EXISTS.
-- ============================================================================

-- Mots-clés bruts normalisés (DataForSEO Labs) ------------------------------
CREATE TABLE IF NOT EXISTS bod.keywords_raw (
  keyword               STRING NOT NULL,
  country_code          STRING,
  location_code         INT64,
  language_code         STRING,
  search_volume         INT64,
  cpc                   FLOAT64   OPTIONS (description = 'devise : USD (donnée du fournisseur)'),
  competition           FLOAT64,
  competition_level     STRING,
  low_top_of_page_bid   FLOAT64,
  high_top_of_page_bid  FLOAT64,
  trend_yoy             FLOAT64   OPTIONS (description = 'croissance annuelle (0.12 = +12 %)'),
  intent                STRING,
  seed                  STRING,
  source                STRING    OPTIONS (description = 'ex. dataforseo'),
  endpoint              STRING,
  data_kind             STRING    OPTIONS (description = 'api_estimate | model_assumption | actual'),
  batch_id              STRING,
  run_id                STRING,
  raw_uri               STRING    OPTIONS (description = 'réponse brute dans Cloud Storage'),
  observed_at           TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY country_code, keyword
OPTIONS (description = 'Mots-clés découverts via DataForSEO (estimations fournisseur)');

-- Besoins regroupés ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS bod.clusters (
  cluster_id        STRING NOT NULL,
  run_id            STRING,
  country_code      STRING,
  language_code     STRING,
  label             STRING,
  n_keywords        INT64,
  total_volume      INT64,
  avg_cpc           FLOAT64 OPTIONS (description = 'USD'),
  weighted_cpc      FLOAT64 OPTIONS (description = 'USD, pondéré par le volume'),
  avg_competition   FLOAT64,
  trend_yoy         FLOAT64,
  dominant_intent   STRING,
  commercial_share  FLOAT64,
  top_keywords      ARRAY<STRING>,
  method            STRING,
  min_cluster_size  INT64,
  embedding_model   STRING,
  source            STRING,
  data_kind         STRING,
  created_at        TIMESTAMP
)
PARTITION BY DATE(created_at)
CLUSTER BY country_code, cluster_id;

CREATE TABLE IF NOT EXISTS bod.cluster_keywords (
  cluster_id     STRING NOT NULL,
  run_id         STRING,
  keyword        STRING NOT NULL,
  country_code   STRING,
  search_volume  INT64,
  cpc            FLOAT64,
  competition    FLOAT64,
  intent         STRING,
  membership     FLOAT64 OPTIONS (description = 'probabilité HDBSCAN'),
  data_kind      STRING,
  created_at     TIMESTAMP
)
PARTITION BY DATE(created_at)
CLUSTER BY cluster_id, keyword;

-- Offres candidates (Gemini) : HYPOTHÈSES -----------------------------------
CREATE TABLE IF NOT EXISTS bod.opportunities (
  opportunity_id           STRING NOT NULL,
  cluster_id               STRING NOT NULL,
  run_id                   STRING,
  country_code             STRING,
  need                     STRING,
  segment                  STRING,
  intent_type              STRING,
  offer_name               STRING,
  business_model           STRING,
  sales_motion             STRING  OPTIONS (description = 'direct | lead'),
  price_hypothesis_eur     FLOAT64 OPTIONS (description = 'hypothèse, prix mensuel si abonnement'),
  margin_rate_hypothesis   FLOAT64,
  lead_to_sale_hypothesis  FLOAT64,
  ltv_months               FLOAT64,
  delivery_effort          STRING,
  risks                    ARRAY<STRING>,
  regulatory_flags         ARRAY<STRING>,
  confidence               STRING,
  model                    STRING,
  prompt_version           STRING,
  source                   STRING,
  data_kind                STRING,
  generated_at             TIMESTAMP
)
PARTITION BY DATE(generated_at)
CLUSTER BY country_code, opportunity_id;

-- Revue humaine (saisie manuelle) : livrable ? prix vérifié ? ---------------
CREATE TABLE IF NOT EXISTS bod.opportunity_reviews (
  opportunity_id         STRING NOT NULL,
  deliverable            BOOL    OPTIONS (description = 'FALSE = éliminatoire'),
  price_override_eur     FLOAT64,
  margin_rate_override   FLOAT64,
  lead_to_sale_override  FLOAT64,
  ltv_months_override    FLOAT64,
  notes                  STRING,
  data_kind              STRING  DEFAULT 'model_assumption',
  reviewed_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
);

-- Scores MOS / PPS et conditions éliminatoires -------------------------------
CREATE TABLE IF NOT EXISTS bod.opportunity_scores (
  opportunity_id          STRING NOT NULL,
  cluster_id              STRING,
  run_id                  STRING,
  country_code            STRING,
  mos_demand              FLOAT64,
  mos_intent              FLOAT64,
  mos_growth              FLOAT64,
  mos_gap                 FLOAT64,
  mos_solvency            FLOAT64,
  mos_size                FLOAT64,
  mos_feasibility         FLOAT64,
  pps_margin              FLOAT64,
  pps_cpa_accessibility   FLOAT64,
  pps_ad_competitiveness  FLOAT64,
  pps_ltv                 FLOAT64,
  pps_reliability         FLOAT64,
  mos                     FLOAT64,
  pps                     FLOAT64,
  priority_score          FLOAT64,
  cpc_eur                 FLOAT64,
  price_eur               FLOAT64,
  margin_rate             FLOAT64,
  lead_to_sale            FLOAT64,
  ltv_months              FLOAT64,
  eliminated              BOOL,
  elimination_reasons     ARRAY<STRING>,
  test_ready              BOOL,
  test_readiness_issues   ARRAY<STRING>,
  source                  STRING,
  data_kind               STRING,
  computed_at             TIMESTAMP
)
PARTITION BY DATE(computed_at)
CLUSTER BY opportunity_id;

-- Annonces, domaines et prix observés (top opportunités uniquement) ---------
CREATE TABLE IF NOT EXISTS bod.serp_snapshots (
  opportunity_id       STRING NOT NULL,
  cluster_id           STRING,
  keyword              STRING NOT NULL,
  country_code         STRING,
  n_paid               INT64,
  n_organic            INT64,
  paa_count            INT64,
  has_shopping         BOOL,
  marketplaces_top10   ARRAY<STRING>,
  marketplace_count    INT64,
  informational_share  FLOAT64,
  price_mentions       ARRAY<FLOAT64>,
  price_median         FLOAT64,
  top_domains          ARRAY<STRING>,
  paid_json            STRING,
  organic_json         STRING,
  run_id               STRING,
  batch_id             STRING,
  raw_uri              STRING,
  cost_usd             FLOAT64,
  source               STRING,
  endpoint             STRING,
  data_kind            STRING,
  observed_at          TIMESTAMP
)
PARTITION BY DATE(observed_at)
CLUSTER BY opportunity_id, keyword;

-- Trois scénarios financiers par opportunité ---------------------------------
CREATE TABLE IF NOT EXISTS bod.scenarios (
  opportunity_id        STRING NOT NULL,
  run_id                STRING,
  scenario              STRING NOT NULL OPTIONS (description = 'pessimistic | central | optimistic'),
  cpc_eur               FLOAT64,
  cr                    FLOAT64 OPTIONS (description = 'conversion clic -> lead/achat'),
  lead_to_sale          FLOAT64,
  refund_rate           FLOAT64,
  price_eur             FLOAT64,
  margin_rate           FLOAT64,
  ltv_months            FLOAT64,
  net_price_eur         FLOAT64,
  contribution          FLOAT64,
  cpa                   FLOAT64,
  profit_per_sale       FLOAT64,
  roas                  FLOAT64,
  max_cpa               FLOAT64,
  breakeven_cr          FLOAT64,
  expected_clicks       FLOAT64 OPTIONS (description = 'avec TEST_BUDGET_EUR'),
  expected_conversions  FLOAT64,
  source                STRING,
  data_kind             STRING,
  computed_at           TIMESTAMP
)
PARTITION BY DATE(computed_at)
CLUSTER BY opportunity_id, scenario;

-- Tests publicitaires réalisés : RÉSULTATS RÉELS ----------------------------
CREATE TABLE IF NOT EXISTS bod.experiments (
  experiment_id   STRING NOT NULL,
  opportunity_id  STRING,
  country_code    STRING,
  start_date      DATE,
  end_date        DATE,
  spend_eur       FLOAT64,
  clicks          INT64,
  impressions     INT64,
  leads           INT64,
  sales           INT64,
  revenue_eur     FLOAT64,
  notes           STRING,
  source          STRING    DEFAULT 'manual',
  data_kind       STRING    DEFAULT 'actual',
  recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP()
)
CLUSTER BY experiment_id;

-- Journal des coûts API ------------------------------------------------------
CREATE TABLE IF NOT EXISTS bod.api_costs (
  day                  DATE,
  endpoint             STRING,
  country_code         STRING,
  cost_usd             FLOAT64,
  run_id               STRING,
  stage                STRING,
  batch_id             STRING,
  tasks_count          INT64,
  status_code          INT64,
  cumulative_cost_usd  FLOAT64,
  max_cost_usd         FLOAT64,
  logged_at            TIMESTAMP
)
PARTITION BY day
CLUSTER BY endpoint, country_code;

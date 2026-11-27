-- ============================================================================
-- BOD Perso - vues pour Looker Studio
--
-- Pages suggérées : 1) classement des opportunités (v_opportunities_ranked)
-- 2) fiche détaillée avec les trois scénarios (v_opportunity_detail)
-- 3) suivi des tests réels (v_experiments_results, v_experiments_vs_forecast)
-- + suivi des coûts (v_api_costs_daily, v_run_costs) et calibrage (v_clusters_review).
-- ============================================================================

-- Dernier score et derniers scénarios connus par opportunité -----------------
CREATE OR REPLACE VIEW bod.v_latest_scores AS
SELECT * FROM bod.opportunity_scores
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY computed_at DESC) = 1;

CREATE OR REPLACE VIEW bod.v_latest_scenarios AS
SELECT * FROM bod.scenarios
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (PARTITION BY opportunity_id, scenario ORDER BY computed_at DESC) = 1;

CREATE OR REPLACE VIEW bod.v_latest_opportunities AS
SELECT * FROM bod.opportunities
WHERE TRUE
QUALIFY ROW_NUMBER() OVER (PARTITION BY opportunity_id ORDER BY generated_at DESC) = 1;

-- Classement des opportunités (conditions éliminatoires appliquées) ---------
CREATE OR REPLACE VIEW bod.v_opportunities_ranked AS
SELECT
  o.opportunity_id,
  o.cluster_id,
  o.need,
  o.segment,
  o.offer_name,
  o.business_model,
  o.sales_motion,
  o.country_code,
  c.label AS cluster_label,
  c.total_volume,
  c.avg_cpc,                          -- USD (fournisseur)
  sc.cpc_eur,
  sc.price_eur,
  sc.mos,
  sc.pps,
  sc.priority_score,
  s_c.cpa            AS cpa_central,
  s_p.cpa            AS cpa_pessimistic,
  s_c.max_cpa,
  s_c.breakeven_cr,
  s_c.profit_per_sale,
  s_p.profit_per_sale AS profit_per_sale_pessimistic,
  s_c.roas           AS roas_central,
  s_c.expected_clicks,
  s_c.expected_conversions,
  sc.test_ready,
  sc.test_readiness_issues,
  o.regulatory_flags,
  o.confidence,
  'model_assumption' AS data_kind,
  sc.computed_at,
  RANK() OVER (ORDER BY sc.priority_score DESC) AS rank_overall,
  RANK() OVER (PARTITION BY o.country_code ORDER BY sc.priority_score DESC) AS rank_in_country
FROM bod.v_latest_opportunities o
JOIN bod.clusters c USING (cluster_id)
JOIN bod.v_latest_scores sc USING (opportunity_id)
JOIN bod.v_latest_scenarios s_c ON s_c.opportunity_id = o.opportunity_id AND s_c.scenario = 'central'
JOIN bod.v_latest_scenarios s_p ON s_p.opportunity_id = o.opportunity_id AND s_p.scenario = 'pessimistic'
WHERE NOT sc.eliminated
  AND s_c.profit_per_sale > 0;      -- condition éliminatoire : marge positive

-- Opportunités écartées et pourquoi -----------------------------------------
CREATE OR REPLACE VIEW bod.v_opportunities_eliminated AS
SELECT o.opportunity_id, o.offer_name, o.need, o.country_code,
       sc.elimination_reasons, sc.mos, sc.pps, sc.computed_at
FROM bod.v_latest_opportunities o
JOIN bod.v_latest_scores sc USING (opportunity_id)
WHERE sc.eliminated;

-- Fiche détaillée : trois scénarios + indicateurs SERP -----------------------
CREATE OR REPLACE VIEW bod.v_opportunity_detail AS
WITH serp AS (
  SELECT opportunity_id,
         COUNT(*)                        AS serp_keywords,
         AVG(n_paid)                     AS avg_paid_ads,
         AVG(marketplace_count)          AS avg_marketplaces_top10,
         AVG(informational_share)        AS avg_informational_share,
         APPROX_QUANTILES(price_median, 2 IGNORE NULLS)[SAFE_OFFSET(1)] AS serp_price_median,
         MAX(observed_at)                AS serp_observed_at
  FROM bod.serp_snapshots
  GROUP BY opportunity_id
)
SELECT
  o.opportunity_id, o.offer_name, o.need, o.segment, o.country_code,
  o.business_model, o.sales_motion, o.delivery_effort, o.risks, o.regulatory_flags,
  s.scenario, s.cpc_eur, s.cr, s.lead_to_sale, s.refund_rate, s.price_eur, s.margin_rate,
  s.ltv_months, s.contribution, s.cpa, s.profit_per_sale, s.roas, s.max_cpa, s.breakeven_cr,
  s.expected_clicks, s.expected_conversions,
  sc.mos, sc.pps, sc.priority_score, sc.eliminated, sc.test_ready,
  serp.serp_keywords, serp.avg_paid_ads, serp.avg_marketplaces_top10,
  serp.avg_informational_share, serp.serp_price_median, serp.serp_observed_at,
  s.data_kind
FROM bod.v_latest_opportunities o
JOIN bod.v_latest_scenarios s USING (opportunity_id)
LEFT JOIN bod.v_latest_scores sc USING (opportunity_id)
LEFT JOIN serp USING (opportunity_id);

-- Calibrage du clustering : 20 plus gros clusters du dernier run par pays ---
CREATE OR REPLACE VIEW bod.v_clusters_review AS
SELECT * EXCEPT (rn) FROM (
  SELECT c.*,
         ROW_NUMBER() OVER (PARTITION BY country_code ORDER BY total_volume DESC) AS rn
  FROM bod.clusters c
  WHERE run_id IN (SELECT MAX_BY(run_id, created_at) FROM bod.clusters GROUP BY country_code)
)
WHERE rn <= 20;

-- Résultats réels des tests publicitaires (data_kind = actual) --------------
CREATE OR REPLACE VIEW bod.v_experiments_results AS
SELECT
  e.experiment_id, e.opportunity_id, e.country_code, e.start_date, e.end_date,
  e.spend_eur, e.clicks, e.impressions, e.leads, e.sales, e.revenue_eur, e.notes,
  SAFE_DIVIDE(e.clicks, e.impressions)  AS ctr,
  SAFE_DIVIDE(e.spend_eur, e.clicks)    AS cpc_real,
  SAFE_DIVIDE(e.leads, e.clicks)        AS cr_click_to_lead,
  SAFE_DIVIDE(e.sales, e.leads)         AS lead_to_sale_real,
  SAFE_DIVIDE(e.spend_eur, e.leads)     AS cost_per_lead,
  SAFE_DIVIDE(e.spend_eur, e.sales)     AS cpa_sale,
  SAFE_DIVIDE(e.revenue_eur, e.spend_eur) AS roas,
  -- Règle de trois : 0 conversion sur n clics => taux réel probablement < 3/n
  IF(e.leads = 0 AND e.clicks > 0, LEAST(1.0, 3 / e.clicks), NULL) AS cr_upper_bound_95,
  e.data_kind
FROM bod.experiments e;

-- Réel vs prévision + lecture des résultats (section 12.4) -------------------
CREATE OR REPLACE VIEW bod.v_experiments_vs_forecast AS
SELECT
  r.*,
  s.cpc_eur      AS cpc_forecast,
  s.cr           AS cr_forecast_central,
  s.breakeven_cr AS breakeven_cr_forecast,
  s.cpa          AS cpa_forecast_central,
  SAFE_DIVIDE(r.cpc_real, s.cpc_eur) AS cpc_real_vs_forecast,
  CASE
    WHEN r.clicks < 30 THEN 'trop_tot'
    WHEN r.clicks <= 50 AND (r.ctr < 0.015 OR SAFE_DIVIDE(r.cpc_real, s.cpc_eur) > 1.5)
      THEN 'revoir_annonces_et_mots_cles'
    WHEN r.clicks >= 100 AND r.leads = 0 THEN 'arreter_ou_changer_offre'
    WHEN r.clicks >= 100 AND r.cr_click_to_lead > s.breakeven_cr THEN 'prometteur_reinvestir_pour_confirmer'
    WHEN r.clicks >= 150 AND r.cr_click_to_lead BETWEEN 0.01 AND s.breakeven_cr
      THEN 'ambigu_ameliorer_page_retester'
    ELSE 'continuer_mesure'
  END AS decision_hint
FROM bod.v_experiments_results r
LEFT JOIN bod.v_latest_scenarios s
  ON s.opportunity_id = r.opportunity_id AND s.scenario = 'central';

-- Coûts API ------------------------------------------------------------------
CREATE OR REPLACE VIEW bod.v_api_costs_daily AS
SELECT day, endpoint, country_code,
       SUM(cost_usd) AS cost_usd,
       COUNT(*)      AS calls,
       SUM(tasks_count) AS tasks
FROM bod.api_costs
GROUP BY day, endpoint, country_code;

-- Coût de collecte par run (critère de réussite phase 1) -------------------
CREATE OR REPLACE VIEW bod.v_run_costs AS
WITH costs AS (
  SELECT run_id, MIN(day) AS day, SUM(cost_usd) AS cost_usd, COUNT(*) AS calls
  FROM bod.api_costs GROUP BY run_id
),
kw AS (
  SELECT run_id, COUNT(*) AS keywords FROM bod.keywords_raw GROUP BY run_id
)
SELECT costs.run_id, costs.day, costs.calls, costs.cost_usd, kw.keywords,
       SAFE_DIVIDE(costs.cost_usd, kw.keywords) AS cost_per_keyword_usd
FROM costs LEFT JOIN kw USING (run_id);

-- Synthèse globale : coût DataForSEO total rapporté aux opportunités --------
CREATE OR REPLACE VIEW bod.v_cost_summary AS
SELECT
  (SELECT SUM(cost_usd) FROM bod.api_costs)                         AS dataforseo_cost_usd,
  (SELECT COUNT(*) FROM bod.keywords_raw)                           AS keywords_collected,
  (SELECT COUNT(*) FROM bod.clusters)                               AS clusters,
  (SELECT COUNT(DISTINCT opportunity_id) FROM bod.opportunities)    AS opportunities,
  SAFE_DIVIDE((SELECT SUM(cost_usd) FROM bod.api_costs),
              (SELECT COUNT(DISTINCT opportunity_id) FROM bod.opportunities)) AS cost_per_opportunity_usd;

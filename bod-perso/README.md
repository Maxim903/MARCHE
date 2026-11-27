# BOD Perso — Business Opportunity Discovery (version personnelle)

**Question :** quel produit ou service commercialiser, sur quel marché, auprès de quelle clientèle et avec quel budget publicitaire, pour maximiser les chances de rentabilité ?

BOD Perso part de données de recherche Google (DataForSEO). Il les regroupe en **besoins**, puis en **opportunités** et en **scénarios de rentabilité**, avant que tu dépenses le moindre euro en publicité. Usage personnel uniquement : un seul projet Google Cloud, BigQuery comme entrepôt unique, Looker Studio pour le tableau de bord. Pas de multi-tenant, pas de facturation, pas d'API publique, pas de frontend.

> Le pipeline **classe et recommande**. Il ne lance **jamais** de campagne Google Ads : tu décides et tu lances toi-même.

## Architecture

```text
Cloud Scheduler (hebdo)
   │
   ▼
Cloud Run Job "bod-collect" ──(secrets)──> Secret Manager
   │
   ├──> DataForSEO API (mots-clés, intention, SERP du top 10)   plafond MAX_COST_USD / exécution
   ├──> Cloud Storage gs://<PROJECT_ID>-raw  (JSON bruts, rejouables sans repayer)
   ▼
BigQuery dataset "bod"
   keywords_raw -> clusters -> opportunities -> scenarios / opportunity_scores -> experiments
        ▲               │
        │               └──> Vertex AI : embeddings + Gemini (offres)
        │
Résultats réels de tes campagnes (saisie manuelle, puis API Google Ads)
   │
   ▼
Vues SQL -> Looker Studio
```

| Fichier | Rôle |
|---|---|
| `app/config.py` | Constantes, marchés, variables d'environnement, régions, modèles, pondérations |
| `app/dataforseo_client.py` | HTTP Basic, retries, **plafond de coût**, sauvegarde brute GCS, journal `bod.api_costs` |
| `app/collect.py` | `keyword_ideas/live` (+ suggestions / related / search_intent en option) vers `bod.keywords_raw` |
| `app/cluster.py` | Filtrage, embeddings Vertex AI et HDBSCAN vers `bod.clusters` / `bod.cluster_keywords` |
| `app/opportunities.py` | Gemini avec sortie JSON **structurée** (schéma imposé) vers `bod.opportunities` |
| `app/simulate.py` | MOS, PPS, conditions éliminatoires et 3 scénarios vers `bod.opportunity_scores` / `bod.scenarios` |
| `app/serp.py` | SERP du **top ~10 uniquement** vers `bod.serp_snapshots` |
| `app/main.py` | Orchestrateur piloté par `STAGE` |
| `app/bq.py` | Helpers BigQuery / Cloud Storage (chargements, sérialisation, chemins bruts) |
| `sql/schema.sql` | Tables du dataset `bod` (partitions et clustering) |
| `sql/views.sql` | Vues Looker Studio, dont `bod.v_opportunities_ranked` et le suivi des `experiments` |

## Principes non négociables

### Traçabilité (`data_kind`)
Chaque ligne porte sa **source**, sa **date d'observation ou de calcul** et sa **nature** :

| `data_kind` | Contenu | Tables |
|---|---|---|
| `api_estimate` | Estimations fournisseur (volume, CPC, SERP) | `keywords_raw`, `clusters`, `cluster_keywords`, `serp_snapshots` |
| `model_assumption` | Hypothèses (Gemini, simulateur, scores, revues) | `opportunities`, `scenarios`, `opportunity_scores`, `opportunity_reviews` |
| `actual` | Résultats réels de campagnes | `experiments` |

Les CPC DataForSEO sont en **USD**. Le simulateur les convertit en EUR avec `USD_TO_EUR`, qui est une hypothèse.

### Contrôle des coûts
- **Plafond par exécution** (`MAX_COST_USD`, ou `--max-cost-usd`). Le champ `cost` de chaque réponse est cumulé. Une fois le plafond atteint, tout nouvel appel lève `BudgetExceeded`. Un appel dont le coût *estimé* (`COST_ESTIMATES_USD`, valeurs majorées) ferait dépasser le plafond est refusé **avant** d'être envoyé.
- Un seul client est partagé par exécution, donc le plafond couvre `collect` et `serp` ensemble quand `STAGE=all`.
- **Le brut d'abord :** chaque réponse est écrite dans `gs://<PROJECT_ID>-raw/raw/<endpoint>/<date>/<batch_id>.json` avant le chargement normalisé. Les mots-clés sont chargés après chaque appel, donc rien de ce qui a été payé n'est perdu si le plafond coupe la suite.
- **Journal :** une ligne par appel dans `bod.api_costs` (jour, endpoint, pays, coût, cumul, plafond, run_id).
- **Entonnoir :** la SERP profonde n'est lancée que sur les meilleures opportunités non éliminées (`SERP_TOP_N=10` × `SERP_KEYWORDS_PER_OPPORTUNITY=3`), avec un plafond dur `SERP_MAX_CALLS=30` et sans rappel d'un mot-clé déjà analysé depuis moins de `SERP_REFRESH_DAYS`.
- `BudgetExceeded` termine le job avec le **code 0** (arrêt volontaire). Cloud Run ne relance donc pas le job et ne redépense pas.
- Garde-fous Vertex AI : `MAX_KEYWORDS_TO_EMBED` et `MAX_CLUSTERS_FOR_LLM`. Les clusters déjà traités ne sont pas renvoyés à Gemini.

## Mise en place Google Cloud (sections 5 et 6 du guide)

```bash
export PROJECT_ID="bod-perso-123"          # à rendre unique
export REGION="europe-west1"
export BQ_LOCATION="EU"

gcloud projects create $PROJECT_ID
gcloud config set project $PROJECT_ID
gcloud billing projects link $PROJECT_ID --billing-account=XXXXXX-XXXXXX-XXXXXX

gcloud services enable bigquery.googleapis.com run.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com aiplatform.googleapis.com \
  storage.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  billingbudgets.googleapis.com

# Bucket de données brutes et dataset BigQuery
gcloud storage buckets create gs://$PROJECT_ID-raw --location=$REGION
bq --location=$BQ_LOCATION mk -d --description "BOD Perso" bod

# Alerte de budget à 50 % et 100 % (devise du compte de facturation)
gcloud billing budgets create --billing-account=XXXXXX-XXXXXX-XXXXXX \
  --display-name="BOD plafond" --budget-amount=30EUR \
  --threshold-rule=percent=0.5 --threshold-rule=percent=1.0
```

### Secrets et compte de service (moindre privilège)

```bash
printf "ton_login_api"    | gcloud secrets create dataforseo-login    --data-file=-
printf "ton_mot_de_passe" | gcloud secrets create dataforseo-password --data-file=-

gcloud iam service-accounts create bod-runner
SA="bod-runner@$PROJECT_ID.iam.gserviceaccount.com"
for ROLE in roles/bigquery.dataEditor roles/bigquery.jobUser roles/storage.objectAdmin \
            roles/secretmanager.secretAccessor roles/aiplatform.user roles/run.invoker; do
  gcloud projects add-iam-policy-binding $PROJECT_ID --member="serviceAccount:$SA" --role="$ROLE"
done
```

Les identifiants DataForSEO ne figurent ni dans le code ni dans le dépôt. Pour un essai local ponctuel, tu peux exporter `DATAFORSEO_LOGIN` et `DATAFORSEO_PASSWORD` dans ton shell. Ne les mets jamais dans un fichier commité.

## Installation locale

```bash
cd bod-perso
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env    # puis adapte PROJECT_ID, etc.
set -a; source .env; set +a
gcloud auth application-default login
```

Crée ensuite les tables et les vues. C'est idempotent, et `bod.` est qualifié avec ton projet et ton dataset :

```bash
python -m app.main --stage init
```

## Exécution

```bash
# 1) Premier appel de contrôle : un seul marché, plafond de quelques centimes
python -m app.main --stage collect --markets FR --max-cost-usd 0.5

# 2) Étapes suivantes
python -m app.main --stage cluster        # puis relire bod.v_clusters_review, ajuster MIN_CLUSTER_SIZE
python -m app.main --stage opportunities
python -m app.main --stage simulate
python -m app.main --stage serp --max-cost-usd 1   # top ~10 seulement

# ou tout d'un coup
STAGE=all python -m app.main
```

Les logs sont émis en JSON (champ `severity`), au format lu par Cloud Logging. Le dernier log `run.done` résume le run, coût DataForSEO compris.

**Réglage du clustering :** `MIN_CLUSTER_SIZE=5` donne des besoins fins, `15` de grandes familles. Examine à la main les 20 clusters de `bod.v_clusters_review` avant de passer à l'échelle.

## Déploiement Cloud Run Job et planification (section 11)

```bash
gcloud run jobs deploy bod-collect --source . --region $REGION \
  --service-account "bod-runner@$PROJECT_ID.iam.gserviceaccount.com" \
  --set-env-vars "^@^PROJECT_ID=$PROJECT_ID@STAGE=collect@MARKETS=FR,US@MAX_COST_USD=5" \
  --task-timeout 3600 --max-retries 0 --memory 2Gi

gcloud run jobs execute bod-collect --region $REGION --wait      # test manuel

# Exécution planifiée : chaque lundi à 06:00
gcloud scheduler jobs create http bod-weekly --location $REGION \
  --schedule "0 6 * * 1" --time-zone "Europe/Paris" --http-method POST \
  --uri "https://$REGION-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT_ID/jobs/bod-collect:run" \
  --oauth-service-account-email "bod-runner@$PROJECT_ID.iam.gserviceaccount.com"
```

- Le guide utilise `--max-retries 1`. Ici, le dépôt recommande `0` : une relance automatique redépenserait jusqu'au plafond.
- Pour lancer tout le pipeline chaque semaine sans redéployer, ajoute à l'appel Scheduler un corps avec override :
  `--message-body '{"overrides":{"containerOverrides":[{"env":[{"name":"STAGE","value":"all"}]}]}}'`.
- Une exécution hebdomadaire suffit. La demande de recherche évolue lentement et chaque appel coûte de l'argent.
- `^@^` change le séparateur de `--set-env-vars`, car `MARKETS` contient une virgule.
- Le marché « Royaume-Uni » utilise le code ISO `GB` (`MARKETS=GB`).

## Scoring et simulation (section 10)

Les deux scores servent à **trier** les opportunités ; ce ne sont pas des modèles prédictifs. Chaque variable est normalisée de 0 à 100 par rang percentile dans le lot. Une donnée manquante vaut 0.

- **MOS (marché)** = 0,15 Demande + 0,20 Intention + 0,15 Croissance + 0,15 Écart offre/demande + 0,15 Solvabilité + 0,10 Taille + 0,10 Faisabilité
- **PPS (rentabilité)** = 0,30 Marge + 0,25 Accessibilité du CPA + 0,20 Compétitivité pub + 0,15 LTV + 0,10 Fiabilité des données
- **Priorité** = 0,5 MOS + 0,5 PPS. Les pondérations sont dans `config.py` et sont à ajuster après tes premiers tests.

| Indicateur | Formule |
|---|---|
| CPA | CPC / (taux de conversion × taux lead→vente) |
| ROAS | revenu net / dépense publicitaire |
| Contribution par vente | prix net après remboursements × taux de marge (× durée si abonnement) |
| Profit par vente | contribution − CPA |
| CPA maximal acceptable | contribution par vente (ou LTV × marge) |
| Conversion minimale (`breakeven_cr`) | CPC / (contribution × taux lead→vente) |
| LTV d'un abonnement | ARPU × marge × durée de vie moyenne |

| Scénario | CPC | Conversion | Remboursements |
|---|---|---|---|
| Pessimiste | × 1,3 | 1 % | 8 % |
| Central | estimé | 3 % | 5 % |
| Optimiste | × 0,8 | 5 % | 3 % |

**Conditions éliminatoires.** Une opportunité est éliminée dans l'un de ces cas :
- marge contributive nulle ou négative ;
- CPA maximal non défini ;
- offre non livrable : effort `high` sans revue humaine, ou `deliverable = FALSE` dans `bod.opportunity_reviews` ;
- restriction réglementaire bloquante : santé, finance, jeux d'argent, alcool, tabac, armes, drogues, adulte, politique ;
- données insuffisantes.

La vue classée exige en plus un profit central positif.

**Critères de test (section 12.1), colonne `test_ready` :** CPC < 2 EUR, CPA max ≥ 30 EUR et budget de test ≥ 3 × CPA max.

### Revue humaine

Gemini propose des idées plausibles, pas des faits. Vérifie le prix, la faisabilité et la réglementation sur le top du classement, puis enregistre ta revue :

```sql
INSERT bod.opportunity_reviews (opportunity_id, deliverable, price_override_eur, notes)
VALUES ('OPP-1A2B3C4D5E', TRUE, 89, 'Prix vérifié chez 3 concurrents');
```

Relance ensuite `--stage simulate`. Les valeurs revues remplacent les hypothèses de Gemini.

## Looker Studio

Crée une source de données avec le connecteur BigQuery et prévois trois pages :
1. **Classement** : `bod.v_opportunities_ranked`, complétée par `v_opportunities_eliminated`.
2. **Fiche détaillée** : `bod.v_opportunity_detail`, avec les trois scénarios et les indicateurs SERP.
3. **Suivi des tests réels** : `bod.v_experiments_results` et `bod.v_experiments_vs_forecast`, qui comparent le réel à la prévision et donnent une indication de décision selon la section 12.4.

Pour le suivi des coûts : `v_api_costs_daily`, `v_run_costs` et `v_cost_summary`.

## Enregistrer un test publicitaire (section 12.5)

```sql
INSERT bod.experiments
  (experiment_id, opportunity_id, country_code, start_date, end_date,
   spend_eur, clicks, impressions, leads, sales, revenue_eur, notes)
VALUES
  ('EXP-001', 'OPP-00012', 'FR', '2026-12-07', '2026-12-18',
   182.40, 131, 4210, 4, 1, 500, 'Page v1, CPC reel 1,39');
```

`data_kind` vaut `'actual'` par défaut. La vue `v_experiments_results` calcule le CPC réel, la conversion clic→lead, le coût par lead, le CPA et le ROAS. Elle calcule aussi la **borne de la règle de trois** : 0 conversion sur n clics signifie un taux réel probablement inférieur à 3/n.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

- `tests/test_simulate.py` : formules CPA, ROAS, contribution, `breakeven_cr`, LTV, règle de trois, trois scénarios (exemples chiffrés du guide), scores percentiles et conditions éliminatoires.
- `tests/test_dataforseo_client.py` : cumul du coût, plafond, refus avant envoi, retries, journalisation.
- `tests/test_parsing.py` : parsing DataForSEO, filtrage, HDBSCAN, indicateurs SERP, sortie Gemini.
- `tests/test_pipeline_offline.py` : enchaînement collect → cluster → opportunities → simulate, avec GCP simulé en mémoire.

## Écarts assumés par rapport aux extraits du guide

- **SDK Vertex AI :** les embeddings passent par `google-genai` (`client.models.embed_content`, `task_type="CLUSTERING"`) au lieu de `vertexai.language_models`, déprécié. `google-cloud-aiplatform` n'est donc plus requis.
- **Scores séparés :** MOS et PPS sont écrits dans `bod.opportunity_scores` au lieu de mettre à jour `bod.opportunities`. Les hypothèses Gemini restent intactes et chaque calcul est historisé.
- **Tables ajoutées :** `bod.opportunity_reviews` pour la revue humaine (livrable, prix vérifié) et des colonnes de traçabilité (`run_id`, `batch_id`, `raw_uri`, `source`, `data_kind`) sur toutes les tables.
- **Entrée Docker :** `python -m app.main` (paquet `app`) au lieu de `python main.py`.

## À revérifier avant exécution

Tarifs, noms de modèles (`GEMINI_MODEL`, `EMBEDDING_MODEL`, disponibilité dans `VERTEX_LOCATION`) et chemins/paramètres des endpoints DataForSEO (limites par requête, disponibilité par pays, `location_code`) doivent être vérifiés dans les documentations officielles. Les estimations `COST_ESTIMATES_USD` ne servent que de garde-fou avant appel : le coût retenu est toujours le champ `cost` de la réponse.

## Conformité

- **RGPD** si ta page de vente collecte des e-mails ou des téléphones.
- Statut juridique et TVA à vérifier avant d'encaisser.
- Conditions d'utilisation de DataForSEO et de Google si tu rediffuses les données.
- N'annonce aucun produit ni délai que tu ne peux pas tenir.

"""Orchestrateur BOD Perso, piloté par la variable STAGE (ou --stage).

Étapes : init | collect | cluster | opportunities | simulate | serp | all
  - init          : crée le dataset, les tables (sql/schema.sql) et les vues (sql/views.sql)
  - collect       : DataForSEO -> GCS brut -> bod.keywords_raw            (payant, plafonné)
  - cluster       : embeddings Vertex AI + HDBSCAN -> clusters / cluster_keywords
  - opportunities : Gemini -> bod.opportunities (hypothèses)
  - simulate      : MOS / PPS / éliminatoires / 3 scénarios -> scores + scenarios
  - serp          : SERP du top ~10 uniquement -> bod.serp_snapshots       (payant, plafonné)
  - all           : collect, cluster, opportunities, simulate, serp

Un seul client DataForSEO est partagé par exécution : le plafond MAX_COST_USD
s'applique à l'ensemble du run. Aucune campagne publicitaire n'est jamais lancée.

Exemples :
  STAGE=collect python -m app.main
  python -m app.main --stage simulate --markets FR
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from pathlib import Path

from app.config import get_settings

STAGES = ("init", "collect", "cluster", "opportunities", "simulate", "serp", "all")
ALL_SEQUENCE = ("collect", "cluster", "opportunities", "simulate", "serp")
SQL_DIR = Path(__file__).resolve().parent.parent / "sql"

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Une ligne JSON par log : lisible par Cloud Logging (champ `severity`)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


log = logging.getLogger("bod")


def stage_init() -> None:
    from google.cloud import bigquery

    from app.bq import bq_client, run_script

    s = get_settings()
    ds = bigquery.Dataset(f"{s.require_project()}.{s.dataset}")
    ds.location = s.bq_location
    ds.description = "BOD Perso - Business Opportunity Discovery"
    bq_client().create_dataset(ds, exists_ok=True)
    for name in ("schema.sql", "views.sql"):
        run_script((SQL_DIR / name).read_text(encoding="utf-8"))
        log.info("init.sql_applied", extra={"file": name})


def run(stage: str, markets: list[str] | None, max_cost_usd: float | None, run_id: str) -> dict:
    s = get_settings()
    s.require_project()
    if stage == "init":
        stage_init()
        return {"init": "ok"}

    sequence = ALL_SEQUENCE if stage == "all" else (stage,)
    api = None
    if {"collect", "serp"} & set(sequence):
        from app.dataforseo_client import DataForSEO

        api = DataForSEO(max_cost_usd=max_cost_usd, run_id=run_id, stage=stage)

    summary: dict = {"run_id": run_id, "stage": stage}
    for step in sequence:
        log.info("stage.start", extra={"stage_name": step, "run_id": run_id})
        if step == "collect":
            from app import collect

            summary[step] = collect.run(api, markets)
        elif step == "cluster":
            from app import cluster

            summary[step] = cluster.run(markets, run_id)
        elif step == "opportunities":
            from app import opportunities

            summary[step] = opportunities.run(markets, run_id)
        elif step == "simulate":
            from app import simulate

            summary[step] = simulate.run(markets, run_id)
        elif step == "serp":
            from app import serp

            summary[step] = serp.run(api, markets)
        log.info("stage.done", extra={"stage_name": step, "result": summary[step]})
    if api is not None:
        summary["dataforseo_spent_usd"] = round(api.spent, 4)
        summary["dataforseo_max_cost_usd"] = api.max_cost
    return summary


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    p = argparse.ArgumentParser(description="BOD Perso - orchestrateur")
    p.add_argument("--stage", default=s.stage, choices=STAGES)
    p.add_argument("--markets", default=None, help="ex. FR,US (défaut : MARKETS)")
    p.add_argument("--max-cost-usd", type=float, default=None, help="plafond DataForSEO de ce run")
    p.add_argument("--run-id", default=None)
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    setup_logging(args.log_level)

    from app.dataforseo_client import BudgetExceeded

    markets = [m.strip().upper() for m in args.markets.split(",")] if args.markets else None
    run_id = args.run_id or uuid.uuid4().hex[:8]
    try:
        summary = run(args.stage, markets, args.max_cost_usd, run_id)
    except BudgetExceeded as exc:
        # Arrêt volontaire : les données déjà payées sont en GCS / BigQuery.
        # Code 0 pour que Cloud Run ne relance pas (et ne redépense pas).
        log.error("budget.exceeded", extra={"detail": str(exc), "run_id": run_id})
        return 0
    except Exception:
        log.exception("run.failed", extra={"run_id": run_id})
        return 1
    log.info("run.done", extra={"summary": summary})
    return 0


if __name__ == "__main__":
    sys.exit(main())

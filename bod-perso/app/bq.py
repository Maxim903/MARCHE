"""Accès BigQuery et Cloud Storage (helpers partagés par toutes les étapes).

Les bibliothèques Google sont importées à la demande pour que les modules
purs (simulate, parsing) restent testables sans dépendances cloud.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
from functools import lru_cache
from typing import Any, Iterable

from app.config import get_settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------
@lru_cache(maxsize=1)
def bq_client():
    from google.cloud import bigquery

    s = get_settings()
    return bigquery.Client(project=s.require_project(), location=s.bq_location)


@lru_cache(maxsize=1)
def gcs_client():
    from google.cloud import storage

    return storage.Client(project=get_settings().require_project())


# --------------------------------------------------------------------------
# Sérialisation sûre (numpy / pandas / NaN -> JSON)
# --------------------------------------------------------------------------
def to_jsonable(value: Any) -> Any:
    """Convertit une valeur en type JSON compatible avec un chargement BigQuery."""
    if value is None:
        return None
    if getattr(value, "ndim", 0) > 0 and hasattr(value, "tolist"):  # numpy array (colonnes ARRAY)
        return [to_jsonable(v) for v in value.tolist()]
    if not isinstance(value, (list, tuple, set, dict, str, bytes)) and not hasattr(value, "tolist"):
        try:  # pandas NA / NaT / numpy NaN
            import pandas as pd

            if pd.isna(value):
                return None
        except (ImportError, TypeError, ValueError):
            pass
    # numpy / pandas scalaires
    if hasattr(value, "item") and not isinstance(value, (list, tuple, dict, str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if isinstance(value, (dt.datetime,)):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if hasattr(value, "tolist"):  # numpy array
        return to_jsonable(value.tolist())
    if hasattr(value, "isoformat"):  # pandas Timestamp
        return value.isoformat()
    return value


def clean_rows(rows: Iterable[dict]) -> list[dict]:
    return [{k: to_jsonable(v) for k, v in r.items()} for r in rows]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------
# BigQuery
# --------------------------------------------------------------------------
def load_rows(table_name: str, rows: list[dict]) -> int:
    """Ajoute des lignes à une table existante via un job de chargement (gratuit).

    Le schéma de la table de destination est imposé (pas d'autodétection) :
    les tables sont créées par sql/schema.sql.
    """
    if not rows:
        return 0
    from google.cloud import bigquery

    client = bq_client()
    table_id = get_settings().table(table_name)
    schema = client.get_table(table_id).schema
    allowed = {f.name for f in schema}
    payload = [{k: v for k, v in r.items() if k in allowed} for r in clean_rows(rows)]
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(payload, table_id, job_config=job_config).result()
    log.info("bq.load", extra={"table": table_id, "rows": len(payload)})
    return len(payload)


def qualify_sql(sql: str) -> str:
    """Remplace `bod.` par `projet.dataset.` (dataset configurable)."""
    import re

    s = get_settings()
    return re.sub(r"(?<![\w.`])bod\.", f"`{s.require_project()}.{s.dataset}`.", sql)


def query(sql: str, params: dict[str, Any] | None = None):
    """Exécute une requête paramétrée et renvoie un DataFrame pandas."""
    from google.cloud import bigquery

    qp = []
    for name, value in (params or {}).items():
        if isinstance(value, (list, tuple)):
            elem_type = "INT64" if value and isinstance(value[0], int) else "STRING"
            qp.append(bigquery.ArrayQueryParameter(name, elem_type, list(value)))
        else:
            qp.append(bigquery.ScalarQueryParameter(name, _bq_type(value), value))
    job = bq_client().query(qualify_sql(sql), job_config=bigquery.QueryJobConfig(query_parameters=qp))
    return job.result().to_dataframe()


def run_script(sql: str) -> None:
    bq_client().query(qualify_sql(sql)).result()


def _bq_type(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT64"
    if isinstance(value, float):
        return "FLOAT64"
    if isinstance(value, dt.datetime):
        return "TIMESTAMP"
    if isinstance(value, dt.date):
        return "DATE"
    return "STRING"


# --------------------------------------------------------------------------
# Cloud Storage (réponses brutes rejouables sans repayer)
# --------------------------------------------------------------------------
def endpoint_slug(path: str) -> str:
    """`dataforseo_labs/google/keyword_ideas/live` -> `dataforseo_labs.google.keyword_ideas.live`."""
    return path.strip("/").replace("/", ".")


def raw_blob_path(endpoint: str, batch_id: str, day: dt.date | None = None) -> str:
    day = day or utcnow().date()
    return f"raw/{endpoint_slug(endpoint)}/{day.isoformat()}/{batch_id}.json"


def ensure_raw_bucket() -> str:
    """Vérifie que le bucket des réponses brutes existe AVANT toute dépense API."""
    s = get_settings()
    name = s.bucket
    if gcs_client().lookup_bucket(name) is None:
        raise RuntimeError(
            f"Le bucket gs://{name} n'existe pas : les réponses brutes ne pourraient pas être "
            f"sauvegardées. Crée-le avant de relancer :\n"
            f"  gcloud storage buckets create gs://{name} --location={s.region}"
        )
    return name


def upload_json(blob_path: str, obj: Any, metadata: dict[str, str] | None = None) -> str:
    bucket = get_settings().bucket
    blob = gcs_client().bucket(bucket).blob(blob_path)
    if metadata:
        blob.metadata = {k: str(v) for k, v in metadata.items()}
    blob.upload_from_string(
        json.dumps(to_jsonable(obj), ensure_ascii=False), content_type="application/json"
    )
    uri = f"gs://{bucket}/{blob_path}"
    log.info("gcs.upload", extra={"uri": uri})
    return uri

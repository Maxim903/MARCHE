"""Client DataForSEO avec contrôle strict des coûts.

Garanties :
- Authentification HTTP Basic ; identifiants lus dans Secret Manager
  (jamais dans le code ni dans le dépôt).
- Plafond de dépense par exécution (`max_cost_usd`) : le champ `cost` de chaque
  réponse est cumulé ; dès que le plafond est atteint, tout nouvel appel lève
  `BudgetExceeded`. Un appel dont le coût estimé ferait dépasser le plafond est
  refusé AVANT d'être envoyé.
- Chaque réponse réussie est d'abord écrite brute dans Cloud Storage
  (`raw/<endpoint>/<date>/<batch_id>.json`), puis le coût est journalisé dans
  `bod.api_costs`. Les données peuvent ainsi être retraitées sans repayer.
- Retries avec backoff exponentiel sur erreurs réseau / 429 / 5xx uniquement.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import requests

from app.config import (
    COST_ESTIMATES_USD,
    DATAFORSEO_BASE_URL,
    DFS_STATUS_OK,
    get_settings,
)

log = logging.getLogger(__name__)

# Codes de statut DataForSEO pour lesquels un nouvel essai a du sens.
RETRYABLE_DFS_CODES = {40202, 40209, 50000, 50301, 50401}
RETRYABLE_HTTP_CODES = {408, 429, 500, 502, 503, 504}


class BudgetExceeded(RuntimeError):
    """Le plafond de dépense DataForSEO de cette exécution est atteint."""


class DataForSEOError(RuntimeError):
    """Erreur non récupérable renvoyée par DataForSEO."""


@dataclass
class ApiResult:
    data: dict
    cost_usd: float
    batch_id: str
    raw_uri: str | None
    endpoint: str
    country_code: str

    @property
    def tasks(self) -> list[dict]:
        return self.data.get("tasks") or []

    def results(self) -> list[dict]:
        """Tous les blocs `result` des tâches réussies."""
        out: list[dict] = []
        for task in self.tasks:
            if task.get("status_code") == DFS_STATUS_OK:
                out.extend(task.get("result") or [])
        return out


def get_secret(name: str, project_id: str | None = None) -> str:
    from google.cloud import secretmanager

    project_id = project_id or get_settings().require_project()
    client = secretmanager.SecretManagerServiceClient()
    path = f"projects/{project_id}/secrets/{name}/versions/latest"
    return client.access_secret_version(name=path).payload.data.decode("utf-8").strip()


def default_auth() -> tuple[str, str]:
    """Identifiants depuis Secret Manager.

    Pour un usage local ponctuel uniquement, DATAFORSEO_LOGIN / DATAFORSEO_PASSWORD
    peuvent être exportées dans le shell (jamais commitées).
    """
    login, password = os.environ.get("DATAFORSEO_LOGIN"), os.environ.get("DATAFORSEO_PASSWORD")
    if login and password:
        log.warning("dfs.auth: identifiants lus depuis l'environnement (usage local)")
        return login, password
    s = get_settings()
    return get_secret(s.dfs_login_secret), get_secret(s.dfs_password_secret)


class DataForSEO:
    def __init__(
        self,
        max_cost_usd: float | None = None,
        *,
        run_id: str | None = None,
        stage: str | None = None,
        auth: tuple[str, str] | None = None,
        session: requests.Session | None = None,
        cost_logger: Callable[[dict], None] | None = None,
        raw_store: Callable[[str, str, dict, dict], str | None] | None = None,
        base_url: str = DATAFORSEO_BASE_URL,
        timeout_s: int | None = None,
        retries: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        s = get_settings()
        self.max_cost = float(max_cost_usd if max_cost_usd is not None else s.max_cost_usd)
        self.spent = 0.0
        self.calls = 0
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.stage = stage or s.stage
        self._auth = auth
        self.session = session or requests.Session()
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s or s.dfs_timeout_s
        self.retries = max(1, retries or s.dfs_retries)
        self._sleep = sleep
        self._cost_logger = cost_logger or self._log_cost_bigquery
        self._raw_store = raw_store or self._store_raw_gcs

    # ------------------------------------------------------------------
    @property
    def auth(self) -> tuple[str, str]:
        if self._auth is None:
            self._auth = default_auth()
        return self._auth

    @property
    def remaining(self) -> float:
        return max(0.0, self.max_cost - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.max_cost

    def check_budget(self, estimated_cost: float | None = None) -> None:
        if self.exhausted:
            raise BudgetExceeded(
                f"Plafond atteint : {self.spent:.4f} / {self.max_cost:.2f} USD"
            )
        if estimated_cost is not None and self.spent + estimated_cost > self.max_cost:
            raise BudgetExceeded(
                f"Appel refusé : {self.spent:.4f} + ~{estimated_cost:.4f} USD estimés "
                f"> plafond {self.max_cost:.2f} USD"
            )

    # ------------------------------------------------------------------
    def post(
        self,
        path: str,
        tasks: list[dict],
        country_code: str = "NA",
        *,
        estimated_cost: float | None = None,
    ) -> ApiResult:
        """POST d'un tableau de tâches. Lève BudgetExceeded / DataForSEOError."""
        path = path.strip("/")
        if estimated_cost is None:
            per_task = COST_ESTIMATES_USD.get(path)
            estimated_cost = per_task * len(tasks) if per_task is not None else None
        self.check_budget(estimated_cost)

        batch_id = f"{country_code.lower()}_{uuid.uuid4().hex[:12]}"
        last_error: str | None = None

        for attempt in range(self.retries):
            if attempt:
                self._sleep(2 ** attempt)
            try:
                r = self.session.post(
                    f"{self.base_url}/{path}", json=tasks, auth=self.auth, timeout=self.timeout_s
                )
            except requests.RequestException as exc:
                last_error = f"réseau: {exc}"
                log.warning("dfs.retry", extra={"path": path, "attempt": attempt, "error": last_error})
                continue

            if r.status_code != 200:
                last_error = f"HTTP {r.status_code}: {r.text[:200]}"
                if r.status_code in RETRYABLE_HTTP_CODES:
                    log.warning("dfs.retry", extra={"path": path, "attempt": attempt, "error": last_error})
                    continue
                raise DataForSEOError(f"Echec DataForSEO {path} : {last_error}")

            data = r.json()
            cost = float(data.get("cost") or 0.0)
            status = data.get("status_code")
            self.calls += 1

            if status == DFS_STATUS_OK:
                # 1) brut d'abord (rejouable), 2) coût, 3) cumul budget
                raw_uri = self._safe_store(path, batch_id, data, country_code)
                self._account(path, country_code, cost, batch_id, len(tasks), status)
                self._warn_task_errors(path, data)
                if self.exhausted:
                    log.warning(
                        "dfs.budget_reached",
                        extra={"spent": self.spent, "max_cost": self.max_cost},
                    )
                return ApiResult(data, cost, batch_id, raw_uri, path, country_code)

            # Erreur globale : un coût éventuel est tout de même comptabilisé.
            if cost:
                self._account(path, country_code, cost, batch_id, len(tasks), status)
            last_error = f"{status}: {data.get('status_message')}"
            if status in RETRYABLE_DFS_CODES and not self.exhausted:
                log.warning("dfs.retry", extra={"path": path, "attempt": attempt, "error": last_error})
                continue
            raise DataForSEOError(f"Echec DataForSEO {path} : {last_error}")

        raise DataForSEOError(f"Echec DataForSEO {path} après {self.retries} essais : {last_error}")

    # ------------------------------------------------------------------
    def _account(self, path, country_code, cost, batch_id, n_tasks, status) -> None:
        self.spent += cost
        row = {
            "day": time.strftime("%Y-%m-%d", time.gmtime()),
            "endpoint": path,
            "country_code": country_code,
            "cost_usd": cost,
            "run_id": self.run_id,
            "stage": self.stage,
            "batch_id": batch_id,
            "tasks_count": n_tasks,
            "status_code": status,
            "cumulative_cost_usd": round(self.spent, 6),
            "max_cost_usd": self.max_cost,
            "logged_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        log.info("dfs.cost", extra=row)
        try:
            self._cost_logger(row)
        except Exception:  # le journal ne doit jamais masquer une dépense
            log.exception("dfs.cost_log_failed", extra=row)

    def _safe_store(self, path, batch_id, data, country_code) -> str | None:
        try:
            return self._raw_store(
                path, batch_id, data, {"country_code": country_code, "run_id": self.run_id}
            )
        except Exception:
            log.exception("dfs.raw_store_failed", extra={"path": path, "batch_id": batch_id})
            return None

    @staticmethod
    def _warn_task_errors(path: str, data: dict) -> None:
        for task in data.get("tasks") or []:
            if task.get("status_code") != DFS_STATUS_OK:
                log.warning(
                    "dfs.task_error",
                    extra={
                        "path": path,
                        "task_status": task.get("status_code"),
                        "status_message": task.get("status_message"),
                    },
                )

    # ---- implémentations par défaut (Google Cloud) -------------------
    @staticmethod
    def _log_cost_bigquery(row: dict) -> None:
        from app.bq import load_rows

        load_rows("api_costs", [row])

    @staticmethod
    def _store_raw_gcs(path: str, batch_id: str, data: dict, metadata: dict) -> str:
        from app.bq import raw_blob_path, upload_json

        return upload_json(raw_blob_path(path, batch_id), data, metadata)

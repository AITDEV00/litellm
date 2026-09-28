"""OICM REST client.

Reads deployment status/health/run facts from the OICM platform. Facts are
returned as raw dicts; normalization into OicmStatusSnapshot lives in
``status_builder`` so this module stays transport-only.

Auth: the OICM tenant is the Keycloak realm. Authenticate against the tenant's
own realm + client (realm == client == tenant name). Token is cached and
refreshed proactively; a single 401 triggers one refresh + one retry. The grant
type is configurable so client-credentials can replace password grant later
without touching call sites.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from .config import (
    OICM_AUTH_GRANT_TYPE,
    OICM_AUTH_URL,
    OICM_BASE_URL,
    OICM_CLIENT_ID,
    OICM_CONCURRENCY,
    OICM_PASSWORD,
    OICM_REALM,
    OICM_TIMEOUT,
    OICM_USERNAME,
)

logger = logging.getLogger("oicm-discovery")

# Refresh the token this many seconds before its stated expiry.
_TOKEN_REFRESH_MARGIN_SECONDS = 30.0


class OicmClient:
    def __init__(
        self,
        base_url: str = OICM_BASE_URL,
        auth_url: str = OICM_AUTH_URL,
        realm: str = OICM_REALM,
        client_id: str = OICM_CLIENT_ID,
        username: str = OICM_USERNAME,
        password: str = OICM_PASSWORD,
        grant_type: str = OICM_AUTH_GRANT_TYPE,
        timeout: float = OICM_TIMEOUT,
        concurrency: int = OICM_CONCURRENCY,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_url = auth_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self._username = username
        self._password = password
        self.grant_type = grant_type
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._auth_lock = asyncio.Lock()

    async def _authenticate(self) -> None:
        url = f"{self.auth_url}/realms/{self.realm}/protocol/openid-connect/token"
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            resp = await client.post(
                url,
                data={
                    "client_id": self.client_id,
                    "username": self._username,
                    "password": self._password,
                    "grant_type": self.grant_type,
                    "scope": "openid",
                },
            )
            resp.raise_for_status()
            body = resp.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("OICM auth response missing access_token")
        expires_in = float(body.get("expires_in", 300))
        self._token = token
        self._token_expiry = time.monotonic() + expires_in - _TOKEN_REFRESH_MARGIN_SECONDS

    async def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with self._auth_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            await self._authenticate()
            return self._token  # type: ignore[return-value]

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expiry = 0.0

    async def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        async with self._semaphore:
            async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
                token = await self._ensure_token()
                resp = await client.get(
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if resp.status_code == 401:
                    self._invalidate_token()
                    token = await self._ensure_token()
                    resp = await client.get(
                        f"{self.base_url}{path}",
                        headers={"Authorization": f"Bearer {token}"},
                        params=params,
                    )
                resp.raise_for_status()
                return resp.json()

    async def get_deployment(self, workspace_id: str, workload_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v1/workspaces/{workspace_id}/deployments/{workload_id}")

    async def get_deployment_health(self, workspace_id: str, workload_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v1/workspaces/{workspace_id}/deployments/{workload_id}/health")

    async def get_workload_run(
        self, workspace_id: str, workload_id: str, workload_run_id: str
    ) -> dict[str, Any]:
        return await self._get(
            f"/api/v1/workspaces/{workspace_id}/workloads/{workload_id}/workload_runs/{workload_run_id}"
        )

    async def list_deployments(self, workspace_id: str) -> dict[str, Any]:
        return await self._get(f"/api/v1/workspaces/{workspace_id}/deployments")

    async def get_inference_metrics_meta(self, workspace_id: str, workload_id: str) -> dict[str, Any]:
        return await self._get(
            f"/api/v1/workspaces/{workspace_id}/deployments/{workload_id}/inference_metrics_meta"
        )

    async def get_inference_metrics(
        self, workspace_id: str, workload_id: str, metric_id: str, start: str
    ) -> Any:
        return await self._get(
            f"/api/v1/workspaces/{workspace_id}/deployments/{workload_id}/inference_metrics",
            params={"metric_id": metric_id, "start": start},
        )

"""OICM-backed ``StatusSource``.

Owns the OICM REST transport (auth, token lifecycle, retries) and validates
responses into the typed ``models`` at the boundary. Everything OICM-specific —
the tenant-realm auth recipe, the URL layout, the ``_version``/``_updated_at``
wire names — is confined here; swap this one class to change backends.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

from ..config import (
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
from .base import StatusSource
from .models import (
    OicmDeployment,
    OicmDeploymentHealth,
    OicmStatusDetail,
    OicmWorkloadRun,
)

logger = logging.getLogger("oicm-discovery")

# Refresh the token this many seconds before its stated expiry.
_TOKEN_REFRESH_MARGIN_SECONDS = 30.0


class OicmStatusSource(StatusSource):
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

    async def _authenticate(self) -> str:
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
        return token

    async def _ensure_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        async with self._auth_lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            return await self._authenticate()

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expiry = 0.0

    async def _get(self, path: str, params: Optional[dict[str, str]] = None) -> Any:
        async with self._semaphore, httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            # Two attempts: the first 401 invalidates the cached token and retries once.
            resp: Optional[httpx.Response] = None
            for attempt in range(2):
                token = await self._ensure_token()
                resp = await client.get(
                    f"{self.base_url}{path}",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
                if resp.status_code != 401 or attempt == 1:
                    break
                self._invalidate_token()
            assert resp is not None  # the loop always runs at least once
            resp.raise_for_status()
            return resp.json()

    def _deployment_path(self, workspace_id: str, workload_id: str) -> str:
        return f"/api/v1/workspaces/{workspace_id}/deployments/{workload_id}"

    async def get_deployment(self, workspace_id: str, workload_id: str) -> OicmDeployment:
        raw = await self._get(self._deployment_path(workspace_id, workload_id))
        return OicmDeployment.model_validate(raw)

    async def get_deployment_health(
        self, workspace_id: str, workload_id: str
    ) -> OicmDeploymentHealth:
        raw = await self._get(f"{self._deployment_path(workspace_id, workload_id)}/health")
        return OicmDeploymentHealth.model_validate(raw)

    async def get_workload_run(
        self, workspace_id: str, workload_id: str, workload_run_id: str
    ) -> Optional[OicmWorkloadRun]:
        raw = await self._get(
            f"/api/v1/workspaces/{workspace_id}/workloads/{workload_id}/workload_runs/{workload_run_id}"
        )
        if not isinstance(raw, dict) or "id" not in raw:
            return None
        raw = dict(raw)
        raw["status_detail"] = tuple(
            OicmStatusDetail.model_validate(e) for e in raw.get("status_detail") or ()
        )
        return OicmWorkloadRun.model_validate(raw)

    async def list_deployments(self, workspace_id: str) -> list[OicmDeployment]:
        raw = await self._get(f"/api/v1/workspaces/{workspace_id}/deployments")
        items = raw.get("items", []) if isinstance(raw, dict) else []
        return [OicmDeployment.model_validate(item) for item in items]

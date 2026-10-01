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
from .wire import OicmDeployment, OicmDeploymentHealth, OicmWorkloadRun

logger = logging.getLogger("oicm-discovery")

# Refresh the token this many seconds before its stated expiry.
_TOKEN_REFRESH_MARGIN_SECONDS = 30.0


class _OicmTokenAuth(httpx.Auth):
    """Keycloak password-grant bearer auth with proactive refresh + one 401 retry.

    Implements the httpx.Auth contract: attaches the cached bearer token, and on
    a 401 refreshes once and re-issues the request. Keeps token plumbing out of
    the request path. Never logs the token or password.
    """

    requires_response_body = True

    def __init__(
        self,
        *,
        auth_url: str,
        realm: str,
        client_id: str,
        username: str,
        password: str,
        grant_type: str,
        timeout: float,
    ):
        self._token_url = f"{auth_url}/realms/{realm}/protocol/openid-connect/token"
        self._payload = {
            "client_id": client_id,
            "username": username,
            "password": password,
            "grant_type": grant_type,
            "scope": "openid",
        }
        self._timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._lock = asyncio.Lock()

    async def _fetch_token(self) -> str:
        async with self._lock:
            if self._token and time.monotonic() < self._token_expiry:
                return self._token
            async with httpx.AsyncClient(timeout=self._timeout, verify=False) as client:
                resp = await client.post(self._token_url, data=self._payload)
                resp.raise_for_status()
                body = resp.json()
            token = body.get("access_token")
            if not token:
                raise RuntimeError("OICM auth response missing access_token")
            self._token = token
            self._token_expiry = (
                time.monotonic() + float(body.get("expires_in", 300)) - _TOKEN_REFRESH_MARGIN_SECONDS
            )
            return token

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._fetch_token()}"
        response = yield request
        if response.status_code != 401:
            return
        # Refresh once and retry the request with the new token.
        self._token = None
        request.headers["Authorization"] = f"Bearer {await self._fetch_token()}"
        yield request


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
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)
        self._auth = _OicmTokenAuth(
            auth_url=auth_url.rstrip("/"),
            realm=realm,
            client_id=client_id,
            username=username,
            password=password,
            grant_type=grant_type,
            timeout=timeout,
        )

    async def _get(self, path: str, params: Optional[dict[str, str]] = None) -> Any:
        async with self._semaphore, httpx.AsyncClient(
            timeout=self.timeout, verify=False, auth=self._auth
        ) as client:
            resp = await client.get(f"{self.base_url}{path}", params=params)
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
        return OicmWorkloadRun.model_validate(raw)

    async def list_deployments(self, workspace_id: str) -> list[OicmDeployment]:
        raw = await self._get(f"/api/v1/workspaces/{workspace_id}/deployments")
        items = raw.get("items", []) if isinstance(raw, dict) else []
        return [OicmDeployment.model_validate(item) for item in items]

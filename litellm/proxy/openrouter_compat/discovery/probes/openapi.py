"""OpenAPI schema probe and route/schema inspector."""

from __future__ import annotations

from typing import cast

from litellm.proxy.openrouter_compat.discovery.probes.base import BaseProbe
from litellm.proxy.openrouter_compat.transport.client import DiscoveryTarget
from litellm.proxy.openrouter_compat.transport.errors import (
    DiscoveryHTTPError,
    DiscoverySchemaError,
)


def _as_dict(value: object) -> dict[str, object] | None:
    return cast(dict[str, object], value) if isinstance(value, dict) else None


class OpenAPIInspector:
    """Minimal OpenAPI inspector resolving local $ref for known request schemas."""

    def __init__(self, document: dict[str, object]) -> None:
        paths = document.get("paths")
        components = document.get("components")
        if not isinstance(paths, dict) or not isinstance(components, dict):
            raise DiscoverySchemaError("openapi document malformed")
        self._paths = cast(dict[str, object], paths)
        self._components = cast(dict[str, object], components)

    def has_path(self, path: str) -> bool:
        return path in self._paths

    def route_paths(self) -> set[str]:
        return {str(path) for path in self._paths}

    def has_operation(self, path: str, method: str) -> bool:
        operation = _as_dict(self._paths.get(path))
        return operation is not None and method.lower() in operation

    def request_schema_mentions(self, path: str, symbol: str) -> bool:
        schema = self._request_schema(path)
        return self._mentions(schema, symbol, seen=set())

    def parameter_names(self, path: str, method: str) -> set[str]:
        operation = _as_dict(self._paths.get(path))
        if operation is None:
            return set()
        entry = _as_dict(operation.get(method.lower()))
        if entry is None:
            return set()
        names: set[str] = set()
        raw_params = entry.get("parameters")
        if isinstance(raw_params, list):
            for param in cast(list[object], raw_params):
                p = _as_dict(param)
                if p is not None and isinstance(p.get("name"), str):
                    names.add(cast(str, p.get("name")))
        return names

    def _request_schema(self, path: str) -> object:
        operation = _as_dict(self._paths.get(path))
        if operation is None:
            return None
        for method in ("post", "put", "patch"):
            entry = _as_dict(operation.get(method))
            if entry is None:
                continue
            body = _as_dict(entry.get("requestBody"))
            if body is None:
                continue
            content = _as_dict(body.get("content"))
            if content is None:
                continue
            media = _as_dict(content.get("application/json"))
            if media is not None:
                return media.get("schema")
        return None

    def _mentions(self, node: object, symbol: str, seen: set[str]) -> bool:
        if isinstance(node, dict):
            for key, value in cast(dict[str, object], node).items():
                if key == "properties":
                    continue
                if key == "$ref" and isinstance(value, str):
                    if value in seen:
                        continue
                    seen.add(value)
                    target = self._resolve_ref(value)
                    if target is not None and self._mentions(target, symbol, seen):
                        return True
                elif key == symbol:
                    return True
                elif isinstance(value, (dict, list)):
                    if self._mentions(cast(object, value), symbol, seen):
                        return True
        elif isinstance(node, list):
            for item in cast(list[object], node):
                if isinstance(item, (dict, list)):
                    if self._mentions(cast(object, item), symbol, seen):
                        return True
        return False

    def _resolve_ref(self, ref: str) -> object | None:
        if not ref.startswith("#/components/schemas/"):
            return None
        name = ref[len("#/components/schemas/") :]
        schemas = _as_dict(self._components.get("schemas"))
        if schemas is None:
            return None
        return schemas.get(name)


class OpenAPISchemaProbe(BaseProbe[OpenAPIInspector]):
    name = "openapi"

    async def _fetch(self, target: DiscoveryTarget) -> OpenAPIInspector:
        try:
            payload = await self._client.get_json(target, "/openapi.json")
        except DiscoveryHTTPError as exc:
            if exc.status_code in (404, 405):
                raise DiscoveryHTTPError(
                    exc.status_code, "openapi unavailable"
                ) from exc
            raise
        return OpenAPIInspector(payload)
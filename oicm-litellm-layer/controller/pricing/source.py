import json
import logging
import time
from pathlib import Path
from typing import Optional

import httpx

from ..config import PRICING_JSON_PATH, PRICING_REFRESH_INTERVAL_SECONDS
from .models import PricingEntry
from .normalizer import normalize_model_name

logger = logging.getLogger("oicm-discovery")

RESERVED_KEYS = frozenset({"sample_spec", "fallback_generalizations"})
INDEXABLE_MODES = frozenset({"chat", "embedding", "completion"})


def _build_entry(key: str, raw: dict) -> Optional[PricingEntry]:
    if key in RESERVED_KEYS:
        return None

    mode = raw.get("mode", "")
    if mode not in INDEXABLE_MODES:
        return None

    input_cost = raw.get("input_cost_per_token")
    output_cost = raw.get("output_cost_per_token")

    if input_cost is None and output_cost is None:
        tiers = raw.get("tiered_pricing")
        if isinstance(tiers, list) and tiers:
            first_tier = tiers[0]
            if isinstance(first_tier, dict):
                input_cost = first_tier.get("input_cost_per_token")
                output_cost = first_tier.get("output_cost_per_token")

    has_pricing = input_cost is not None or output_cost is not None

    if has_pricing:
        ic = float(input_cost) if input_cost is not None else 0.0
        oc = float(output_cost) if output_cost is not None else 0.0
        if ic == 0.0 and oc == 0.0:
            has_pricing = False

    return PricingEntry(
        key=key,
        input_cost_per_token=float(input_cost) if input_cost is not None else 0.0,
        output_cost_per_token=float(output_cost) if output_cost is not None else 0.0,
        has_pricing=has_pricing,
    )


def _load_from_file(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


async def _load_from_proxy(base_url: str, headers: dict) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{base_url}/model/info", headers=headers)
        resp.raise_for_status()
        result: dict[str, dict] = {}
        for m in resp.json().get("data", []):
            info = m.get("model_info", {})
            model_name = m.get("model_name", "")
            if not model_name or not isinstance(info, dict):
                continue
            input_cost = info.get("input_cost_per_token")
            if input_cost is not None:
                entry: dict = {
                    "mode": info.get("mode", "chat"),
                    "input_cost_per_token": input_cost,
                }
                output_cost = info.get("output_cost_per_token")
                if output_cost is not None:
                    entry["output_cost_per_token"] = output_cost
                result[model_name] = entry
        return result


class PricingIndex:
    __slots__ = ("by_normalized_key", "entries", "skipped_no_pricing")

    def __init__(
        self,
        entries: dict[str, PricingEntry],
        by_normalized_key: dict[str, PricingEntry],
        skipped_no_pricing: int,
    ):
        self.entries = entries
        self.by_normalized_key = by_normalized_key
        self.skipped_no_pricing = skipped_no_pricing

    def __len__(self) -> int:
        return len(self.entries)

    def __bool__(self) -> bool:
        return bool(self.entries)


def _build_index(raw_map: dict) -> PricingIndex:
    entries: dict[str, PricingEntry] = {}
    by_normalized_key: dict[str, PricingEntry] = {}
    skipped = 0

    for key, raw in raw_map.items():
        if not isinstance(raw, dict):
            continue
        entry = _build_entry(key, raw)
        if entry is None:
            continue
        if not entry.has_pricing:
            skipped += 1
            continue
        entries[key] = entry
        norm = normalize_model_name(key)
        if norm and norm not in by_normalized_key:
            by_normalized_key[norm] = entry

    logger.info(
        "Pricing index built: %d entries indexed, %d skipped (no pricing)",
        len(entries),
        skipped,
    )
    return PricingIndex(entries, by_normalized_key, skipped)


class PricingSource:
    def __init__(
        self,
        base_url: str = "",
        headers: Optional[dict] = None,
        json_path: str = PRICING_JSON_PATH,
        refresh_interval: int = PRICING_REFRESH_INTERVAL_SECONDS,
    ):
        self._base_url = base_url
        self._headers = headers or {}
        self._json_path = json_path
        self._refresh_interval = refresh_interval
        self._index: Optional[PricingIndex] = None
        self._last_load: float = 0.0

    async def get_index(self) -> PricingIndex:
        if self._index is not None and not self._is_stale():
            return self._index

        raw_map = await self._load_raw()
        if raw_map:
            self._index = _build_index(raw_map)
            self._last_load = time.monotonic()
        elif self._index is not None:
            logger.warning(
                "Pricing JSON reload failed; serving stale index (%d entries)",
                len(self._index),
            )
        else:
            logger.warning("Pricing JSON unavailable; index is empty")
            self._index = PricingIndex({}, {}, 0)
        return self._index

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._last_load) > self._refresh_interval

    async def _load_raw(self) -> dict:
        try:
            return _load_from_file(self._json_path)
        except FileNotFoundError:
            logger.debug(
                "Pricing JSON not found at %s, trying proxy", self._json_path
            )
        except Exception as e:
            logger.warning("Failed to load pricing JSON from %s: %s", self._json_path, e)

        if self._base_url:
            try:
                return await _load_from_proxy(self._base_url, self._headers)
            except Exception as e:
                logger.warning("Failed to load pricing from proxy %s: %s", self._base_url, e)

        return {}

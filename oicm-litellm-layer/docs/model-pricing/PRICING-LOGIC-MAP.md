# Pricing Logic Map — Function-Level Trace

## Entry Points

Two paths trigger pricing resolution:

### Path A: Event-driven (`controller.py:_handle_add`)

```
K8s watch event (model added)
    |
    v
[DiscoveryController._handle_add] (controller.py:197)
    |
    +-- detect_mode(model_id, extra_args) -> mode
    +-- if mode == "tts_skip": return (skip entirely)
    |
    v
[self.pricing_resolver.resolve(model.model_id)] (resolver.py:24)
    |
    v
[pricing_to_params(pricing)] (utils.py:6)
    |
    v
[self.litellm.register_model(model, inherited)] (litellm_client.py:96)
    |
    v
[self.litellm.batch([], [(model, inherited)], [])] (litellm_client.py:74)
    |
    v
[self._register_one(client, model, inherited)] (litellm_client.py:130)
    |
    +-- litellm_params = {model, api_base, api_key, drop_params}
    +-- for k,v in inherited.items():
    |       if k not in litellm_params and v is not None:
    |           litellm_params[k] = v
    |
    v
POST /model/new with litellm_params including input_cost_per_token + output_cost_per_token
```

### Path B: Sync reconciliation (`reconciler.py:compute_plan`)

```
SyncReconciler.compute_plan(k8s_models, litellm_by_uuid) (reconciler.py:52)
    |
    +-- For NEW models (uuid in k8s, not in litellm):
    |       pricing = await self.pricing.resolve(model.model_id)
    |       plan.registers.append((model, pricing_to_params(pricing)))
    |
    +-- For EXISTING models (uuid in both):
    |       if existing_model_name != model.model_name:
    |           pricing = await self.pricing.resolve(model.model_id)
    |           plan.registers.append((model, pricing_to_params(pricing)))
    |       else:
    |           plan.patches.append((existing_id, {model, api_base}))  # BUG: no pricing
    |
    v
[litellm.batch(deletes, registers, patches)] (litellm_client.py:66)
```

## Resolver Internal Flow

```
PricingResolver.resolve(model_id) (resolver.py:24)
    |
    +-- if not PRICING_ENABLED or not model_id: return None
    |
    v
[source.get_index()] (source.py:149)
    |
    +-- if cached and not stale: return cached
    +-- _load_raw() -> _load_from_file() or _load_from_proxy()
    +-- _build_index(raw_map) (source.py:107)
    |       |
    |       +-- for each key, raw in raw_map:
    |       |       _build_entry(key, raw) (source.py:19)
    |       |       |
    |       |       +-- skip RESERVED_KEYS (sample_spec, fallback_generalizations)
    |       |       +-- skip if mode not in INDEXABLE_MODES (chat, embedding, completion)
    |       |       +-- extract input_cost/output_cost (flat or tiered_pricing first tier)
    |       |       +-- has_pricing = input_cost is not None or output_cost is not None
    |       |       +-- if chat and both costs == 0.0: has_pricing = False
    |       |       +-- return PricingEntry(key, provider, mode, costs, has_pricing, source)
    |       |
    |       +-- skip entries where not has_pricing
    |       +-- entries[key] = entry
    |       +-- by_normalized_key[normalize_model_name(key)] = entry (first wins)
    |
    v
normalized = normalize_model_name(model_id) (normalizer.py:14)
    |
    +-- strip HF org prefix: rsplit("/", 1)[-1]
    +-- strip bedrock dotted prefix: if prefix.isalpha() and "-" in rest: strip
    +-- lowercase
    +-- strip suffixes (instruct, turbo, fp8, v1, dates, :0) in loop
    +-- replace _ and space with -, collapse repeated -
    +-- strip leading/trailing -
    |
    v
for matcher in DEFAULT_MATCHERS: (resolver.py:37)
    |
    +-- exact_match(normalized, index.by_normalized_key) (matchers.py:10)
    |       index.get(normalized) -> score 1.0
    |
    +-- structured_match(normalized, index.by_normalized_key) (matchers.py:32)
    |       parse_family_and_params(normalized) -> (family, param_count)
    |       for each entry: parse_family_and_params(normalize_model_name(entry.key))
    |       family + param match -> score 0.95
    |       family only -> score 0.70
    |
    +-- fuzzy_match(normalized, index.by_normalized_key) (matchers.py:79)
    |       token pre-filter (must share at least 1 token)
    |       difflib.SequenceMatcher.ratio() >= 0.80
    |
    +-- substring_match(normalized, index.by_normalized_key) (matchers.py:117)
    |       MIN_SUBSTRING_LENGTH = 5
    |       bidirectional: model in key OR key in model
    |       score 0.85
    |
    v
aggregate(candidates, model_id, threshold) (aggregator.py:7)
    |
    +-- dedup by json_key, keep highest score
    +-- if any score >= 1.0: return that one directly (exact short-circuit)
    +-- filter by threshold (default 0.80)
    +-- if 1 candidate: return directly
    +-- if >1 candidate: weighted average by score, strategy="aggregated"
    +-- if 0 candidates: return None
    |
    v
PricingResult(input_cost, output_cost, matched_keys, aggregate_score, strategy)
```

## Data Contracts

### PricingEntry (models.py:5)
```python
key: str                     # original JSON key
litellm_provider: str        # e.g. "deepseek", "bedrock_converse"
mode: str                    # "chat", "embedding", "completion"
input_cost_per_token: float  # USD per token
output_cost_per_token: float # USD per token
has_pricing: bool            # False if no pricing fields or zero-cost chat
source_url: str              # from "source" field in JSON
```

### PricingResult (models.py:25)
```python
input_cost_per_token: float
output_cost_per_token: float
matched_keys: tuple[str, ...]   # JSON keys that matched
aggregate_score: float          # min score among candidates (for aggregated)
strategy: str                   # matcher_name or "aggregated"
```

### pricing_to_params output (utils.py:6)
```python
{"input_cost_per_token": float, "output_cost_per_token": float}
# or None if result is None
```

### LiteLLM _register_one inherited_params merge (litellm_client.py:151)
```python
for k, v in inherited_params.items():
    if k not in litellm_params and v is not None:
        litellm_params[k] = v
```
NOTE: `v is not None` check means a cost of 0.0 WILL be set. This is correct for
embedding models (output_cost=0.0 is valid), but means zero-cost entries that
slip through the filter will set $0 pricing.
```

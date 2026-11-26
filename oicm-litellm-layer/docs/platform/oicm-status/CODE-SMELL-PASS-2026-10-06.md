# Code smell pass: controller (2026-10-06)

Technique: `docs/techniques/code_smell_detection_technique.md`. Scope:
`oicm-litellm-layer/controller/` and `tests/controller/`.

Result: 14 findings fixed, 3 findings recorded as deliberate. 278 tests pass
(the 2 `test_config.py` failures are pre-existing and unrelated).

## L1 — automated baseline

Tools: pyflakes, vulture (`--min-confidence 80`), ruff
(`F,E9,PLC,PLE,PLR,PLW,B,SIM,RET`).

### Fixed

| Finding | Where | What |
|---|---|---|
| `B905` zip without `strict=` | `reconciler.py:292`, `status_persister.py:244` | Both zips are positionally aligned by construction (a failed register leaves a `None` placeholder). Added `strict=True` so a length mismatch is a loud error rather than a silent misalignment. |
| `PLW2901` loop variable overwritten | `controller.py:237` | The loop rebound `model` to a `replace()` copy, so the name meant two things. Now `discovered` is the loop variable and `model` is derived once. |
| Unused import | `__main__.py:6` | Already carried `# noqa: F401` with a reason (configures logging on import). Left as is; the noqa is the correct signal. |
| `PLR0911` too many returns | `models.py:166` | `detect_mode_from_paths` was 12 ifs. Refactored to two ordered tables (`_PATH_MODES`, `_NAME_MODES`) plus one loop, which also makes the precedence explicit instead of positional. |

### Recorded as deliberate

| Finding | Where | Why it stays |
|---|---|---|
| `PLR0912` / `PLR0915` (22 branches, 64 statements) | `reconciler.compute_plan` | The decision table is the reconciliation policy itself. Splitting it into helpers would scatter the ordering that the correctness depends on (a key known only to OICM must be handled before the register loop). The branches are the domain, not incidental nesting. |
| vulture `unused variable 'cls'` (100%) | `wire.py:106` | False positive. It is a pydantic `@field_validator` `@classmethod` signature; pydantic supplies `cls`. |

## L2 — per-file checklist

### Fixed

| Finding | Where | What |
|---|---|---|
| Dead method `deregister_model` | `litellm_client.py` | No production consumer. `_handle_delete` pauses instead of removing, and the reconciler deletes through `batch()` directly. Only a test called it. Removed the method and its test, and the dead stub and assertions in `test_controller_watch.py`. |
| Dead property `is_ready` | `models.py` | `ready_replicas > 0` off the k8s watch. Superseded by OICM `serving_available`; no consumer. Removed. |
| Dead field `source_updated_at` | `snapshot.py`, `builder.py`, `wire.py` | Populated from OICM `_updated_at` and read by nothing. The design explicitly documents that timestamp as unreliable. Removed from the snapshot, the builder, and the wire model (the `_updated_at` key still parses, since `extra="allow"`). |
| Dead field `previous_source_status` | `snapshot.py`, `builder.py` | Carried the prior status for a consumer that never appeared; `status_changed_at` is what tracks transitions. Removed. |
| Dead enum `WorkloadStatus` | `snapshot.py` | No production consumer, and it is not a member of any dispatch table. Removed from the module and from the package `__all__`. |
| Unused import `Optional` | `sources/base.py` | Cascade from my own edit. Removed. |
| Duplicate `except Exception` | `submariner_imports.py:141` | Cascade from my own edit: an old handler tail was left after a `return`. Removed. |
| Unused import `pytest` | `test_config.py` | Removed. |

### Recorded as deliberate

| Finding | Why it stays |
|---|---|
| `logger.error` rather than `logger.exception` in 18 handlers | Every one is a deliberate degrade-and-continue path in an HTTP client: a failed per-model op must not abort the reconcile. The message carries the response body, which is the useful signal. A traceback for an expected `ConnectError` is noise. |
| `BLE001` blind `except Exception` | Same reason: the client's contract is to degrade on *any* transport error, which is exactly what a narrow catch would fail to do. |
| Most methods without docstrings | The codebase convention is docstrings where intent is non-obvious. The ones added in this pass cover the non-obvious cases (the write guard, the heartbeat, the join). |

## L3 — cross-reference analysis

This is the layer that found the two real bugs.

### Fixed

| Finding | Where | What |
|---|---|---|
| Enum member not in any consumer | `WorkloadStatus` | Cross-check of every enum against its consumers showed all 7 members had none. See L2. |
| Field populated but never read | `source_updated_at`, `previous_source_status` | Traced the write path for every `OicmStatusSnapshot` field; both were written by the builder and read by nobody. |
| Stale docstring naming a nonexistent class | `wire.py:91` | Referenced an ``OicmDeployment`` that does not exist anywhere. Reworded to describe the actual reason the model is separate. |
| Stale docstring naming a nonexistent field | `oicm.py:6` | Claimed the wire names ``_version``/``_updated_at`` were confined there; neither is used. Reworded. |
| Stale reference to a removed signal | `availability.py:12` | Cited ``/health.is_ready``, which the design dropped. Reworded. |
| Duplicated logic across sources | `_query_v1_models` in both sources | Two near-identical implementations of the same probe with the same 405 handling. Extracted to `probe_v1_models` in `sources/base.py`, so the two cannot diverge on the 405 rule. Added three tests covering the shape, the 405, and the error propagation. |

### Recorded as deliberate

| Finding | Why it stays |
|---|---|
| `CLUSTER_NAME` unused inside `controller/` | It is a deployment-facing knob: it is read by `models.py` as the default for `OicmModel.cluster` and set in both manifests. Its absence from other modules is correct. |
| `DeploymentStatus` members with no consumer | The enum models OICM's own vocabulary, which is the point of a wire-facing enum. `PENDING`, `UNDEPLOYING` etc. are what OICM can report, and `_enum_or_none` maps them through. Only `STOPPED`/`FAILED` are consulted for serving, which is the intended asymmetry. |

## L4 — recheck

Re-ran all three L1 tools after the fixes. Two cascades were introduced by the
fixes themselves and both were caught and fixed: an unused `Optional` import in
`sources/base.py`, and a duplicated `except` clause left behind in
`submariner_imports.py`. Final state is clean apart from the three deliberate
findings above.

## Notes on the misnamed predicate

`status/availability.py::_is_ready` is renamed `_entry_is_serving`. The name was
left over from the dropped `/health.is_ready` signal and had come to mean the
opposite of what it says: a `Ready` deployment whose pod is out of service
returns False. The behaviour is unchanged and still covered by the existing
tests.

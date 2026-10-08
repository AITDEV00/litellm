"""The spend-logs janitor manifests must not reintroduce the bugs that broke prod.

Prod went 24h with every janitor run failing because the partitions it selected
were owned by a role its DB user is not a member of. The scripts were fine and
dev passed, because dev's partitions all happen to be owned by dev's DB user.
These tests pin the parts that a dev-only run cannot exercise: the verify step's
ownership replay, the bootstrap's ownership loop covering detached partitions,
and default privileges for the roles that create partitions.

Run with the controller's test extra:

    cd oicm-litellm-layer && uv run --extra test --python 3.13 \
        python -m pytest tests/deploy/test_janitor_manifests.py
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import yaml

_LAYER_ROOT: Final = Path(__file__).resolve().parents[2]
_JANITORS: Final = {
    "dev": _LAYER_ROOT / "deploy" / "dev" / "spend-logs-janitor",
    "prod": _LAYER_ROOT / "deploy" / "prod" / "spend-logs-janitor",
}


def _documents(env: str) -> list[dict]:
    text = (_JANITORS[env] / "cronjob.yaml").read_text()
    return [d for d in yaml.safe_load_all(text) if isinstance(d, dict)]


def _script(env: str) -> str:
    configmap = next(d for d in _documents(env) if d["kind"] == "ConfigMap")
    return configmap["data"]["janitor.sh"]


def _cronjob(env: str) -> dict:
    return next(d for d in _documents(env) if d["kind"] == "CronJob")


def _bootstrap(env: str) -> str:
    return (_JANITORS[env] / "sql" / "bootstrap.sql").read_text()


@pytest.mark.parametrize("env", sorted(_JANITORS))
def test_pg_restore_does_not_replay_ownership(env: str):
    """pg_dump emits `OWNER TO <partition owner>`, so pg_restore would fail for a
    partition owned by a role the janitor's DB user is not a member of. --no-owner
    makes the verify step independent of who owns the partition."""
    restore = [line for line in _script(env).splitlines() if "PGRESTORE" in line and "dbname=" in line]
    assert restore, f"{env}: no pg_restore invocation found"
    assert all("--no-owner" in line for line in restore), restore


@pytest.mark.parametrize("env", sorted(_JANITORS))
def test_bootstrap_chowns_detached_partitions(env: str):
    """The ownership loop must match partitions by name over pg_class, not walk
    pg_partition_tree. A partition the janitor detached but has not dropped is no
    longer a child of the master, so a tree walk skips it and the next run fails
    to read it. The name filter <master>_p% covers attached partitions, _pdefault
    and detached ones alike."""
    sql = _bootstrap(env)
    loop = sql.split("FOR r IN", 1)[1].split("LOOP", 1)[0]
    assert "FROM pg_class" in loop, f"{env}: detached partitions are absent from pg_partition_tree"
    assert "relname LIKE master" in loop, loop


@pytest.mark.parametrize("env", sorted(_JANITORS))
def test_bootstrap_grants_default_privileges_to_every_partition_creating_role(env: str):
    """Partitions created by a role with no default privileges are owned by that
    role alone, which is exactly how prod accumulated 64 postgres-owned
    partitions the janitor could not read."""
    sql = _bootstrap(env)
    for role in ("litellm", "postgres"):
        assert re.search(rf"ALTER DEFAULT PRIVILEGES FOR ROLE {role}\b", sql), (
            f"{env}: missing default privileges for role {role}"
        )


def test_dev_and_prod_scripts_differ_only_in_the_archive_free_space_floor():
    """The two environments run the same state machine. Any other divergence means
    a fix landed in one place only, which is how prod and dev drift apart."""
    dev = _script("dev").splitlines()
    prod = _script("prod").splitlines()
    assert len(dev) == len(prod), "scripts have different lengths"
    differing = [(d, p) for d, p in zip(dev, prod) if d != p]
    assert len(differing) == 1, differing
    line = differing[0][0]
    assert line.startswith("ARCHIVE_MIN_FREE_GB="), line


@pytest.mark.parametrize("env", sorted(_JANITORS))
def test_bootstrap_never_drops_data(env: str):
    """bootstrap.sql runs as DDL only. A DROP or DELETE here would destroy spend
    logs on the next apply, before the janitor has archived them."""
    sql = _bootstrap(env)
    executable = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert not re.search(r"\bDROP\s+(TABLE|DATABASE)\b", executable, re.IGNORECASE), executable
    assert not re.search(r"\bDELETE\s+FROM\b", executable, re.IGNORECASE), executable

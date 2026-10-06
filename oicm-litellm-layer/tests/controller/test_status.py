"""Tests for the OICM status DTOs and availability mapping.

These pin behavior, not structure: each test would fail if the wire parsing or
the availability decision regressed, independent of how the modules are split.
The summary-item cases are driven by the live captures in
``docs/oicm-status/evidence/deployment-summary.json``.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from controller.status import (
    DeploymentStatus,
    OicmDeploymentSummary,
    build_snapshot,
    is_deployment_available,
)

_EVIDENCE = Path(__file__).resolve().parents[2] / "docs" / "oicm-status" / "evidence"


def _summary(status, status_detail=(), replicas=1, error_msg=None):
    return OicmDeploymentSummary.model_validate(
        {
            "deployment_id": "dep1",
            "status": status,
            "replicas": replicas,
            "error_msg": error_msg,
            "status_detail": list(status_detail),
            "_updated_at": "2026-09-18T14:06:27Z",
        }
    )


@pytest.fixture(scope="module")
def evidence_items():
    """The live deployment_summary capture: Ready, Deploying, and Stopped."""
    raw = json.loads((_EVIDENCE / "deployment-summary.json").read_text())
    return [OicmDeploymentSummary.model_validate(i) for i in raw["items"]]


def _pod(ready, node="gpu-01"):
    return {"kind": "Pod", "node": node, "metadata": {"ready": ready}}


def _build(summary, previous=None):
    return build_snapshot(
        workspace_id="ws1",
        summary=summary,
        previous=previous,
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


class TestWireParsing:
    def test_discriminates_each_kind(self):
        summary = _summary(
            "Ready",
            [
                {"kind": "Deployment", "metadata": {"available": True, "available_replicas": 1}},
                {"kind": "Pod", "node": "gpu-01", "metadata": {"ready": True}},
                {"kind": "LeaderWorkerSet", "metadata": {"available": True}},
            ],
        )
        assert [type(e).__name__ for e in summary.status_detail] == [
            "DeploymentStatusDetail",
            "PodStatusDetail",
            "LeaderWorkerSetStatusDetail",
        ]

    def test_unknown_kind_is_dropped_not_fatal(self):
        summary = _summary(
            "Ready",
            [
                {"kind": "Pod", "node": "gpu-01", "metadata": {"ready": True}},
                {"kind": "FutureKindOicmMightAdd", "metadata": {"ready": True}},
            ],
        )
        assert [type(e).__name__ for e in summary.status_detail] == ["PodStatusDetail"]

    def test_tolerates_missing_and_null_fields(self):
        summary = _summary("Ready", [{"kind": "Pod", "metadata": {"ready": True}, "apiVersion": None}])
        assert summary.status_detail[0].node is None

    def test_extra_fields_are_kept(self):
        summary = _summary(
            "Ready",
            [{"kind": "Pod", "node": "n", "metadata": {"ready": True}, "brand_new_field": 1}],
        )
        assert getattr(summary.status_detail[0], "brand_new_field") == 1

    def test_unavailable_replicas_is_typed(self):
        # The Deploying fixture carries unavailable_replicas; it must be a
        # typed field, not something only reachable via extra="allow".
        summary = _summary(
            "Deploying",
            [{"kind": "Deployment", "metadata": {"progressing": True, "unavailable_replicas": 1}}],
        )
        entry = summary.status_detail[0]
        assert entry.metadata.unavailable_replicas == 1


class TestAvailability:
    def test_pod_ready_with_node_is_available(self):
        assert is_deployment_available(_summary("Ready", [_pod(True)]).status_detail) is True

    def test_pod_ready_without_node_is_not_available(self):
        assert is_deployment_available(_summary("Ready", [_pod(True, node="")]).status_detail) is False

    def test_pod_not_ready_is_not_available(self):
        assert is_deployment_available(_summary("Ready", [_pod(False)]).status_detail) is False

    def test_api_version_null_does_not_block_pod(self):
        # Regression guard for the OICM defect: apiVersion is always null, and
        # must not be a precondition for availability.
        detail = [{"kind": "Pod", "apiVersion": None, "node": "gpu-01", "metadata": {"ready": True}}]
        assert is_deployment_available(_summary("Ready", detail).status_detail) is True

    def test_leader_worker_set_available(self):
        detail = [{"kind": "LeaderWorkerSet", "metadata": {"available": True}}]
        assert is_deployment_available(_summary("Ready", detail).status_detail) is True

    def test_deployment_entry_alone_is_not_available(self):
        detail = [{"kind": "Deployment", "metadata": {"available": True, "available_replicas": 1}}]
        assert is_deployment_available(_summary("Ready", detail).status_detail) is False

    def test_empty_status_detail_is_not_available(self):
        assert is_deployment_available(_summary("Ready", []).status_detail) is False


class TestAbuDhabiMissingMetadata:
    """OICM 1.7.1 (Abu Dhabi) does not populate ``status_detail[].metadata``.

    Live payload, deployment 766b1720, OICM status ``Ready``, pod Running on
    ``prd-infr-k8h200``. Requiring ``metadata.ready`` makes this read as not
    serving, which would pause a healthy model once Abu Dhabi is targeted. The
    entry's own ``status`` carries the same fact and both versions populate it.
    """

    AD_READY = [
        {"kind": "Deployment", "name": "j-766b1720", "node": None, "status": "Ready",
         "status_msg": "1/1 replicas ready"},
        {"kind": "Pod", "name": "j-766b1720-767bd4657b-lzfmt", "node": "prd-infr-k8h200",
         "status": "Running", "status_msg": None},
    ]

    def test_running_pod_without_metadata_is_serving(self):
        assert is_deployment_available(_summary("Ready", self.AD_READY).status_detail) is True

    def test_snapshot_reports_serving_for_the_abu_dhabi_payload(self):
        snap = _build(_summary("Ready", self.AD_READY))
        assert snap.source_status is DeploymentStatus.READY
        assert snap.serving_available is True

    def test_metadata_still_wins_when_present(self):
        """A pod with metadata ready=false is not serving even if status says Running.

        The fallback only applies when metadata is absent, so a version that does
        report readiness keeps its more precise answer.
        """
        detail = [{"kind": "Pod", "node": "gpu-01", "status": "Running", "metadata": {"ready": False}}]
        assert is_deployment_available(_summary("Ready", detail).status_detail) is False

    def test_unscheduled_pod_without_metadata_is_not_serving(self):
        """A pod with no node cannot serve, whatever its status says."""
        detail = [{"kind": "Pod", "node": None, "status": "Running"}]
        assert is_deployment_available(_summary("Ready", detail).status_detail) is False

    def test_non_serving_status_without_metadata_is_not_serving(self):
        """A Deploying pod is Running but the rollout is not done."""
        detail = [{"kind": "Pod", "node": "gpu-01", "status": "Pending"}]
        assert is_deployment_available(_summary("Ready", detail).status_detail) is False

    def test_leader_worker_set_without_metadata_falls_back_to_status(self):
        detail = [{"kind": "LeaderWorkerSet", "status": "Available"}]
        assert is_deployment_available(_summary("Ready", detail).status_detail) is True

    def test_stopped_without_metadata_is_not_serving(self):
        snap = _build(_summary("Stopped", self.AD_READY))
        assert snap.serving_available is False


class TestBuildSnapshot:
    def test_ready_deployment_marks_serving_available(self):
        snap = _build(_summary("Ready", [_pod(True), {"kind": "Deployment", "metadata": {"available_replicas": 1}}]))
        assert snap.source_status is DeploymentStatus.READY
        assert snap.serving_available is True
        assert snap.available_replicas == 1

    def test_stopped_is_not_available_even_with_empty_status_detail(self):
        # Live: a Stopped deployment returns an empty status_detail, so the
        # STOPPED short-circuit (not the status_detail scan) is what makes this
        # correct.
        snap = _build(_summary("Stopped", []))
        assert snap.source_status is DeploymentStatus.STOPPED
        assert snap.serving_available is False

    def test_failed_is_not_available(self):
        snap = _build(_summary("Failed", []))
        assert snap.serving_available is False

    def test_deploying_is_not_serving_with_unavailable_replicas(self):
        # Live Deploying fixture: pod on a node but metadata has no ready flag.
        summary = _summary(
            "Deploying",
            [
                {"kind": "Pod", "node": "adeo-gpu-01", "metadata": {}},
                {"kind": "Deployment", "metadata": {"progressing": True, "unavailable_replicas": 1}},
            ],
        )
        snap = _build(summary)
        assert snap.source_status is DeploymentStatus.DEPLOYING
        assert snap.serving_available is False
        assert snap.unavailable_replicas == 1

    def test_unknown_status_string_maps_to_none_not_crash(self):
        snap = _build(_summary("BrandNewStatusOicmAdded", []))
        assert snap.source_status is None
        assert snap.serving_available is False

    def test_wire_alias_populates_updated_at(self):
        snap = _build(_summary("Ready", []))
        assert snap.source_updated_at == "2026-09-18T14:06:27Z"

    def test_available_status_with_ready_run_is_serving(self):
        snap = _build(_summary("Available", [_pod(True)]))
        assert snap.source_status is DeploymentStatus.AVAILABLE
        assert snap.serving_available is True

    def test_error_msg_is_surfaced(self):
        snap = _build(_summary("Failed", [], error_msg="image pull backoff"))
        assert snap.error_msg == "image pull backoff"


class TestEvidenceFixture:
    """Every status present in the live capture must be viewable."""

    def test_fixture_covers_the_observed_statuses(self, evidence_items):
        statuses = {i.status for i in evidence_items}
        assert {"Ready", "Deploying"} <= statuses

    def test_every_item_produces_a_snapshot_with_a_status(self, evidence_items):
        for item in evidence_items:
            snap = _build(item)
            assert snap.source_status is not None
            assert snap.workload_id == item.deployment_id
            expected = snap.source_status in (DeploymentStatus.READY, DeploymentStatus.AVAILABLE)
            assert snap.serving_available is expected

    def test_stopped_lifecycle_phase_is_not_serving(self):
        # The Stopped state for the same deployment is a different moment, so it
        # lives in the lifecycle capture, not the single-moment summary.
        lc = json.loads((_EVIDENCE / "lifecycle-transitions.json").read_text())
        for phase in lc["phases"]:
            dep = phase["deployment"]
            snap = _build(
                OicmDeploymentSummary.model_validate(
                    {
                        "deployment_id": dep["deployment_id"],
                        "status": dep["status"],
                        "replicas": dep["replicas"],
                        "error_msg": dep["error_msg"],
                        "status_detail": dep["status_detail"],
                    }
                )
            )
            assert snap.source_status.value == dep["status"]
            assert snap.serving_available is False


class TestTransitionMemory:
    def test_status_changed_at_is_stable_when_nothing_changes(self):
        first = _build(_summary("Ready", [_pod(True)]))
        later = build_snapshot(
            workspace_id="ws1",
            summary=_summary("Ready", [_pod(True)]),
            previous=first,
            now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        assert later.status_changed_at == first.status_changed_at
        assert later.previous_source_status is DeploymentStatus.READY

    def test_serving_flip_moves_status_changed_at_despite_same_source_status(self):
        # Regression guard: OICM can keep reporting "Ready" while the pod drops
        # out of service. That transition must move status_changed_at.
        first = _build(_summary("Ready", [_pod(True)]))
        assert first.serving_available is True
        degraded = build_snapshot(
            workspace_id="ws1",
            summary=_summary("Ready", [_pod(False)]),
            previous=first,
            now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        assert degraded.source_status is DeploymentStatus.READY
        assert degraded.serving_available is False
        assert degraded.status_changed_at != first.status_changed_at
        assert degraded.status_changed_at == degraded.observed_at

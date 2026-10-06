"""Tests for the OICM status DTOs and availability mapping.

These pin behavior, not structure: each test would fail if the wire parsing or
the availability decision regressed, independent of how the modules are split.
"""

from datetime import datetime, timezone

from controller.status import (
    DeploymentStatus,
    OicmDeployment,
    OicmDeploymentHealth,
    OicmWorkloadRun,
    build_snapshot,
    is_deployment_available,
)


def _run(status_detail, workload_status="Running"):
    return OicmWorkloadRun.model_validate(
        {"id": "run1", "workload_status": workload_status, "status_detail": status_detail}
    )


def _deployment(status="Ready"):
    return OicmDeployment.model_validate(
        {"id": "dep1", "status": status, "replicas": 1, "_version": 6, "_updated_at": "2026-09-18T14:06:27Z"}
    )


def _health():
    return OicmDeploymentHealth.model_validate(
        {"is_health_check_supported": True, "is_ready": True, "message": "ok"}
    )


def _build(deployment, health, run, previous=None):
    return build_snapshot(
        workspace_id="ws1",
        workload_id="dep1",
        workload_run_id=run.id if run else None,
        deployment=deployment,
        health=health,
        workload_run=run,
        previous=previous,
        now=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


class TestWireParsing:
    def test_discriminates_each_kind(self):
        run = _run(
            [
                {"kind": "Deployment", "metadata": {"available": True, "available_replicas": 1}},
                {"kind": "Pod", "node": "gpu-01", "metadata": {"ready": True}},
                {"kind": "LeaderWorkerSet", "metadata": {"available": True}},
            ]
        )
        assert [type(e).__name__ for e in run.status_detail] == [
            "DeploymentStatusDetail",
            "PodStatusDetail",
            "LeaderWorkerSetStatusDetail",
        ]

    def test_unknown_kind_is_dropped_not_fatal(self):
        run = _run(
            [
                {"kind": "Pod", "node": "gpu-01", "metadata": {"ready": True}},
                {"kind": "FutureKindOicmMightAdd", "metadata": {"ready": True}},
            ]
        )
        assert [type(e).__name__ for e in run.status_detail] == ["PodStatusDetail"]

    def test_tolerates_missing_and_null_fields(self):
        run = _run([{"kind": "Pod", "metadata": {"ready": True}, "apiVersion": None}])
        assert run.status_detail[0].node is None

    def test_extra_fields_are_kept(self):
        run = _run([{"kind": "Pod", "node": "n", "metadata": {"ready": True}, "brand_new_field": 1}])
        assert getattr(run.status_detail[0], "brand_new_field") == 1


class TestAvailability:
    def test_pod_ready_with_node_is_available(self):
        run = _run([{"kind": "Pod", "node": "gpu-01", "metadata": {"ready": True}}])
        assert is_deployment_available(run.status_detail) is True

    def test_pod_ready_without_node_is_not_available(self):
        run = _run([{"kind": "Pod", "metadata": {"ready": True}}])
        assert is_deployment_available(run.status_detail) is False

    def test_pod_not_ready_is_not_available(self):
        run = _run([{"kind": "Pod", "node": "gpu-01", "metadata": {"ready": False}}])
        assert is_deployment_available(run.status_detail) is False

    def test_api_version_null_does_not_block_pod(self):
        # Regression guard for the OICM defect: apiVersion is always null, and
        # must not be a precondition for availability.
        run = _run([{"kind": "Pod", "apiVersion": None, "node": "gpu-01", "metadata": {"ready": True}}])
        assert is_deployment_available(run.status_detail) is True

    def test_leader_worker_set_available(self):
        run = _run([{"kind": "LeaderWorkerSet", "metadata": {"available": True}}])
        assert is_deployment_available(run.status_detail) is True

    def test_deployment_entry_alone_is_not_available(self):
        run = _run([{"kind": "Deployment", "metadata": {"available": True, "available_replicas": 1}}])
        assert is_deployment_available(run.status_detail) is False

    def test_empty_status_detail_is_not_available(self):
        run = _run([])
        assert is_deployment_available(run.status_detail) is False


class TestReadyPodCount:
    def test_counts_only_ready_pods(self):
        run = _run(
            [
                {"kind": "Pod", "node": "a", "metadata": {"ready": True}},
                {"kind": "Pod", "node": "b", "metadata": {"ready": False}},
                {"kind": "Deployment", "metadata": {"available": True}},
            ]
        )
        assert run.ready_pod_count == 1


class TestBuildSnapshot:
    def test_ready_deployment_marks_serving_available(self):
        snap = _build(_deployment("Ready"), _health(), _run([{"kind": "Pod", "node": "n", "metadata": {"ready": True}}]))
        assert snap.source_status is DeploymentStatus.READY
        assert snap.serving_available is True
        assert snap.available_replicas == 1
        assert snap.is_ready is True

    def test_stopped_is_not_available_even_with_run(self):
        snap = _build(
            _deployment("Stopped"),
            _health(),
            _run([{"kind": "Pod", "node": "n", "metadata": {"ready": True}}], workload_status="Completed"),
        )
        assert snap.source_status is DeploymentStatus.STOPPED
        assert snap.serving_available is False

    def test_failed_is_not_available(self):
        snap = _build(_deployment("Failed"), _health(), None)
        assert snap.serving_available is False

    def test_no_run_means_unknown_availability(self):
        snap = _build(_deployment("Ready"), _health(), None)
        assert snap.serving_available is None
        assert snap.available_replicas is None

    def test_unknown_status_string_maps_to_none_not_crash(self):
        snap = _build(_deployment("BrandNewStatusOicmAdded"), _health(), None)
        assert snap.source_status is None
        # unknown status is not STOPPED/FAILED, and with no run -> None
        assert snap.serving_available is None

    def test_wire_aliases_populate_version_and_updated_at(self):
        snap = _build(_deployment("Ready"), _health(), None)
        assert snap.source_version == 6
        assert snap.source_updated_at == "2026-09-18T14:06:27Z"

    def test_available_status_with_ready_run_is_serving(self):
        snap = _build(
            _deployment("Available"),
            _health(),
            _run([{"kind": "Pod", "node": "n", "metadata": {"ready": True}}]),
        )
        assert snap.source_status is DeploymentStatus.AVAILABLE
        assert snap.serving_available is True

    def test_missing_health_leaves_is_ready_none(self):
        snap = _build(_deployment("Ready"), None, None)
        assert snap.is_ready is None
        assert snap.health_supported is False
        assert snap.health_message is None


class TestTransitionMemory:
    def test_status_changed_at_is_stable_when_nothing_changes(self):
        first = _build(
            _deployment("Ready"),
            _health(),
            _run([{"kind": "Pod", "node": "n", "metadata": {"ready": True}}]),
        )
        later = build_snapshot(
            workspace_id="ws1",
            workload_id="dep1",
            workload_run_id="run1",
            deployment=_deployment("Ready"),
            health=_health(),
            workload_run=_run([{"kind": "Pod", "node": "n", "metadata": {"ready": True}}]),
            previous=first,
            now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        assert later.status_changed_at == first.status_changed_at
        assert later.previous_source_status is DeploymentStatus.READY
        assert later.previous_workload_run_id == "run1"

    def test_serving_flip_moves_status_changed_at_despite_same_source_status(self):
        # Regression guard: OICM can keep reporting "Ready" while the pod drops
        # out of service. That transition must move status_changed_at.
        first = _build(
            _deployment("Ready"),
            _health(),
            _run([{"kind": "Pod", "node": "n", "metadata": {"ready": True}}]),
        )
        assert first.serving_available is True
        degraded = build_snapshot(
            workspace_id="ws1",
            workload_id="dep1",
            workload_run_id="run1",
            deployment=_deployment("Ready"),
            health=_health(),
            workload_run=_run([{"kind": "Pod", "node": "n", "metadata": {"ready": False}}]),
            previous=first,
            now=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        assert degraded.source_status is DeploymentStatus.READY
        assert degraded.serving_available is False
        assert degraded.status_changed_at != first.status_changed_at
        assert degraded.status_changed_at == degraded.observed_at

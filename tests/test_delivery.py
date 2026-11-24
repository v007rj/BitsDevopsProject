import json
import subprocess
from unittest.mock import Mock

import pytest
import yaml

from scripts.deploy import Cluster, bootstrap, deploy, restore, snapshot
from scripts.routing import ROOT, nginx_config, router_resources, workload
from scripts.sonar_report import collect
from scripts.sonar_report import main as report_main


@pytest.mark.parametrize("percentage", [-1, 101, True, "10"])
def test_invalid_canary_percentage_is_rejected(percentage):
    with pytest.raises(ValueError):
        nginx_config(percentage)


@pytest.mark.parametrize("image", ['name:tag"\ninvalid: yaml', "bad image", "x"])
def test_manifest_rejects_unsafe_image_substitution(image):
    with pytest.raises(ValueError):
        workload("blue", image, "2.0.0")


def test_manifests_keep_shared_data_and_zero_unavailable_replicas():
    base = list(yaml.safe_load_all((ROOT / "k8s" / "base.yaml").read_text()))
    assert any(obj["kind"] == "PersistentVolumeClaim" for obj in base)
    for slot in ("blue", "green", "candidate", "rolling"):
        manifest = yaml.safe_load(workload(slot, "owner/aceest:2.0.0", "2.0.0"))
        spec = manifest["spec"]
        assert spec["strategy"]["rollingUpdate"]["maxUnavailable"] == 0
        pod = spec["template"]["spec"]
        assert pod["nodeSelector"] == {"aceest-storage": "true"}
        assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "aceest-data"
        assert pod["containers"][0]["readinessProbe"]["httpGet"]["path"] == "/health"


def test_route_change_creates_new_immutable_config_and_keeps_old_available():
    initial, _ = router_resources(0)
    canary, manifest = router_resources(10)
    assert initial["metadata"]["name"] != canary["metadata"]["name"]
    assert canary["immutable"] is True
    pod = yaml.safe_load(manifest)["spec"]["template"]["spec"]
    assert pod["volumes"][0]["configMap"]["name"] == canary["metadata"]["name"]


def cluster_fixture():
    cluster = Mock(spec=Cluster)
    cluster.context = "test-cluster"
    objects = {
        ("deployment", f"aceest-{slot}"): yaml.safe_load(
            workload(slot, "owner/aceest:1.0.0", "1.0.0")
        )
        for slot in ("blue", "green", "candidate", "rolling")
    }
    objects[("deployment", "aceest-router")] = yaml.safe_load(router_resources()[1])
    objects[("service", "aceest-stable")] = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": "aceest-stable"},
        "spec": {"selector": {"app": "aceest-fitness", "slot": "blue"}},
    }
    cluster.get.side_effect = lambda kind, name: objects[(kind, name)]
    return cluster


def test_failed_rollout_restores_previous_images_and_routes(tmp_path):
    cluster = cluster_fixture()
    failure = subprocess.CalledProcessError(1, "kubectl rollout status")
    cluster.wait.side_effect = [failure, None, None, None, None, None]
    with pytest.raises(subprocess.CalledProcessError):
        deploy(cluster, "owner/aceest:2.0.0", "2.0.0", "blue-green", tmp_path, 0)
    applied = [call.args[0] for call in cluster.apply.call_args_list[1:]]
    assert len(applied) == 6
    restored_green = next(
        json.loads(doc)
        for doc in applied
        if json.loads(doc)["metadata"]["name"] == "aceest-green"
    )
    container = restored_green["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "owner/aceest:1.0.0"
    cluster.probe.assert_any_call("/api/version", "1.0.0")
    assert (tmp_path / "rollback.json").exists()
    assert not (tmp_path / "deployment.json").exists()


def test_rollback_refuses_different_cluster():
    cluster = cluster_fixture()
    saved = snapshot(cluster)
    cluster.context = "wrong-cluster"
    with pytest.raises(ValueError, match="context differs"):
        restore(cluster, saved)
    cluster.apply.assert_not_called()


def test_blue_green_updates_only_inactive_slot_and_switches_after_health(tmp_path):
    cluster = cluster_fixture()
    deploy(cluster, "owner/aceest:2.0.0", "2.0.0", "blue-green", tmp_path, 0)
    desired = yaml.safe_load(cluster.apply.call_args.args[0])
    assert desired["metadata"]["name"] == "aceest-green"
    cluster.run.assert_called_once()
    assert json.loads(cluster.run.call_args.args[-1]) == {
        "spec": {"selector": {"slot": "green"}}
    }
    events = [call[0] for call in cluster.mock_calls]
    assert events.index("probe") < events.index("run")


def test_bootstrap_refuses_to_overwrite_existing_release():
    cluster = cluster_fixture()
    cluster.run.return_value = "deployment.apps/aceest-blue"
    with pytest.raises(ValueError, match="already initialized"):
        bootstrap(cluster, "owner/aceest:2.0.0", "2.0.0")
    cluster.apply.assert_not_called()


def test_sonar_report_uses_exact_analysis_gate(monkeypatch):
    calls = []

    def fetch(_base, endpoint, _token, parameters):
        calls.append((endpoint, parameters))
        if endpoint == "ce/task":
            return {
                "task": {
                    "status": "SUCCESS",
                    "analysisId": "analysis-123",
                    "componentKey": "aceest-fitness",
                }
            }
        if endpoint == "qualitygates/project_status":
            return {"projectStatus": {"status": "ERROR"}}
        if endpoint == "issues/search":
            return {"issues": [], "total": 0}
        return {"component": {"measures": [{"metric": "bugs", "value": "1"}]}}

    monkeypatch.setattr("scripts.sonar_report.fetch", fetch)
    result = collect("http://sonar", "not-a-real-token", "task-123")
    assert result["qualityGate"]["status"] == "ERROR"
    assert ("qualitygates/project_status", {"analysisId": "analysis-123"}) in calls


def test_failed_sonar_background_task_is_not_a_success_report(monkeypatch):
    monkeypatch.setattr(
        "scripts.sonar_report.fetch",
        lambda *args: {"task": {"status": "FAILED"}},
    )
    with pytest.raises(RuntimeError, match="FAILED"):
        collect("http://sonar", "not-a-real-token", "task-123")


@pytest.mark.parametrize(
    ("status", "conditions", "message"),
    [
        ("ERROR", [{"status": "ERROR"}], "gate failed"),
        ("OK", [], "no conditions"),
    ],
)
def test_exporter_saves_evidence_but_blocks_failed_or_empty_gate(
    monkeypatch, tmp_path, status, conditions, message
):
    task_file = tmp_path / "task.txt"
    task_file.write_text("ceTaskId=task-123\n")
    output = tmp_path / "sonar.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "sonar_report",
            "--task-file",
            str(task_file),
            "--output",
            str(output),
        ],
    )
    monkeypatch.setenv("SONAR_HOST_URL", "http://sonar")
    monkeypatch.setenv("SONAR_TOKEN", "not-a-real-token")
    monkeypatch.setattr(
        "scripts.sonar_report.collect",
        lambda *args: {
            "qualityGate": {"status": status, "conditions": conditions},
        },
    )
    with pytest.raises(SystemExit, match=message):
        report_main()
    assert json.loads(output.read_text())["qualityGate"]["status"] == status

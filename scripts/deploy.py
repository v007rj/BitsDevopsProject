"""Transactional Minikube deployment with persisted, explicit rollback."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

from scripts.routing import ROOT, SLOTS, router_resources, workload

STRATEGIES = ("rolling", "blue-green", "canary", "shadow", "ab")


class Cluster:
    def __init__(self, context):
        self.context = context

    def run(self, *args, document=None):
        command = ["kubectl", "--context", self.context, "-n", "aceest", *args]
        try:
            result = subprocess.run(
                command, input=document, text=True, capture_output=True, check=True
            )
        except subprocess.CalledProcessError as error:
            print(error.stderr, file=sys.stderr, flush=True)
            raise
        return result.stdout

    def apply(self, document):
        result = self.run("apply", "-f", "-", document=document)
        print(result, end="", flush=True)
        return result

    def wait(self, name):
        print(
            self.run("rollout", "status", f"deployment/{name}", "--timeout=180s"),
            end="",
            flush=True,
        )

    def get(self, kind, name):
        return json.loads(self.run("get", kind, name, "-o", "json"))

    def route(self, percentage):
        configmap, deployment = router_resources(percentage)
        self.apply(json.dumps(configmap))
        self.apply(deployment)
        self.wait("aceest-router")

    def probe(self, path, expected_version=None, service="aceest-router", attempts=15):
        port = 80 if service == "aceest-router" else 5000
        for attempt in range(attempts):
            try:
                raw = self.run(
                    "get",
                    "--raw",
                    f"/api/v1/namespaces/aceest/services/{service}:{port}/proxy{path}",
                )
                payload = json.loads(raw)
                if expected_version is not None:
                    if payload.get("version") != expected_version:
                        raise ValueError(f"Unexpected release from {path}: {payload}")
                elif payload.get("status") != "healthy":
                    raise ValueError(f"Unhealthy response from {path}: {payload}")
                return payload
            except (subprocess.CalledProcessError, ValueError):
                if attempt == attempts - 1:
                    raise
                time.sleep(2)
        raise RuntimeError("Probe did not run.")


def snapshot(cluster):
    objects = [cluster.get("deployment", f"aceest-{slot}") for slot in SLOTS] + [
        cluster.get("deployment", "aceest-router"),
        cluster.get("service", "aceest-stable"),
    ]
    return {
        "context": cluster.context,
        "objects": [
            {
                "apiVersion": obj["apiVersion"],
                "kind": obj["kind"],
                "metadata": {
                    "name": obj["metadata"]["name"],
                    "namespace": "aceest",
                },
                "spec": obj["spec"],
            }
            for obj in objects
        ],
    }


def restore(cluster, saved):
    if saved["context"] != cluster.context:
        raise ValueError("Rollback context differs from the saved deployment context.")
    for obj in saved["objects"]:
        cluster.apply(json.dumps(obj))
    for obj in saved["objects"]:
        if obj["kind"] == "Deployment":
            cluster.wait(obj["metadata"]["name"])
    cluster.probe("/health")
    previous_slot = next(
        obj["spec"]["selector"]["slot"]
        for obj in saved["objects"]
        if obj["kind"] == "Service"
    )
    previous_deployment = next(
        obj
        for obj in saved["objects"]
        if obj["metadata"]["name"] == f"aceest-{previous_slot}"
    )
    previous_version = next(
        env["value"]
        for env in previous_deployment["spec"]["template"]["spec"]["containers"][0][
            "env"
        ]
        if env["name"] == "APP_VERSION"
    )
    cluster.probe("/api/version", previous_version)


def check_weight(cluster, percentage, version):
    code = """
import collections, json, urllib.request
counts = collections.Counter()
for _ in range(200):
    with urllib.request.urlopen(
        'http://aceest-router/canary/api/version', timeout=10
    ) as response:
        counts[json.load(response)['slot']] += 1
print(json.dumps(counts))
"""
    counts = json.loads(
        cluster.run("exec", "deployment/aceest-candidate", "--", "python", "-c", code)
    )
    if percentage == 100:
        if counts != {"candidate": 200}:
            raise ValueError(f"100% canary did not reach only the candidate: {counts}")
        cluster.probe("/canary/api/version", version)
    elif not counts.get("candidate") or sum(counts.values()) == counts["candidate"]:
        raise ValueError(f"Both stable and canary must receive traffic: {counts}")
    print(f"Canary {percentage}%: {counts}", flush=True)


def bootstrap(cluster, image, version):
    existing = cluster.run(
        "get", "deployment", "aceest-blue", "--ignore-not-found", "-o", "name"
    )
    if existing.strip():
        raise ValueError("Cluster already initialized; use deploy, not bootstrap.")
    cluster.apply((ROOT / "k8s" / "base.yaml").read_text())
    for slot in SLOTS:
        cluster.apply(workload(slot, image, version))
        cluster.wait(f"aceest-{slot}")
    cluster.route(0)
    for prefix in ("", "/rolling", "/blue-green", "/canary", "/shadow", "/ab"):
        cluster.probe(f"{prefix}/health")
    cluster.probe("/api/version", version)


def deploy(cluster, image, version, strategy, evidence, pause):
    saved = snapshot(cluster)
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "rollback.json").write_text(json.dumps(saved, indent=2))
    stable_slot = cluster.get("service", "aceest-stable")["spec"]["selector"]["slot"]
    target = (
        "rolling"
        if strategy == "rolling"
        else ("green" if stable_slot == "blue" else "blue")
        if strategy == "blue-green"
        else "candidate"
    )
    try:
        cluster.apply(workload(target, image, version))
        cluster.wait(f"aceest-{target}")
        cluster.probe("/health", service=f"aceest-{target}")
        cluster.probe("/api/version", version, service=f"aceest-{target}")
        if strategy == "blue-green":
            cluster.run(
                "patch",
                "service",
                "aceest-stable",
                "--type=merge",
                "-p",
                json.dumps({"spec": {"selector": {"slot": target}}}),
            )
        elif strategy == "canary":
            for percentage in (10, 50, 100):
                cluster.route(percentage)
                check_weight(cluster, percentage, version)
                time.sleep(pause)
            cluster.apply(workload(stable_slot, image, version))
            cluster.wait(f"aceest-{stable_slot}")
            cluster.probe("/api/version", version, service="aceest-stable")
            cluster.route(0)
        elif strategy in ("shadow", "ab"):
            cluster.route(0)
        prefix = f"/{strategy}"
        cluster.probe(f"{prefix}/health")
        if strategy in ("rolling", "blue-green", "canary"):
            cluster.probe(f"{prefix}/api/version", version)
        else:
            cluster.probe(
                "/api/version", service="aceest-candidate", expected_version=version
            )
        (evidence / "deployment.json").write_text(
            json.dumps(
                {
                    "strategy": strategy,
                    "image": image,
                    "version": version,
                    "context": cluster.context,
                    "result": "success",
                    "stable_slot": cluster.get("service", "aceest-stable")["spec"][
                        "selector"
                    ]["slot"],
                },
                indent=2,
            )
        )
    except (subprocess.CalledProcessError, ValueError, OSError) as error:
        print(
            f"Deployment failed: {error}. Restoring the saved release.", file=sys.stderr
        )
        restore(cluster, saved)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "deploy", "rollback", "render"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--image")
    parser.add_argument("--version")
    parser.add_argument("--strategy", choices=STRATEGIES, default="rolling")
    parser.add_argument("--evidence", type=Path, default=Path("artifacts"))
    parser.add_argument("--rollback-file", type=Path)
    parser.add_argument("--pause", type=int, default=15)
    args = parser.parse_args()
    cluster = Cluster(args.context)
    if args.action == "rollback":
        if args.rollback_file is None:
            parser.error("--rollback-file is required.")
        restore(cluster, json.loads(args.rollback_file.read_text()))
        return
    if not args.image or not args.version:
        parser.error("--image and --version are required.")
    if args.pause < 0:
        parser.error("--pause must not be negative.")
    # Validate substitutions before any mutation of Kubernetes objects.
    workload("blue", args.image, args.version)
    if args.action == "render":
        print((ROOT / "k8s" / "base.yaml").read_text())
        for slot in SLOTS:
            print("---\n" + workload(slot, args.image, args.version))
        configmap, router = router_resources()
        print("---\n" + json.dumps(configmap))
        print("---\n" + router)
    elif args.action == "bootstrap":
        bootstrap(cluster, args.image, args.version)
    else:
        deploy(
            cluster, args.image, args.version, args.strategy, args.evidence, args.pause
        )


if __name__ == "__main__":
    main()

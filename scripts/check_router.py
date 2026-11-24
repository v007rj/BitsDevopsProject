"""Exercise production routing with real disposable Docker containers."""

import argparse
import collections
import concurrent.futures
import json
import subprocess
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from scripts.routing import nginx_config


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def request(base, path, payload=None, headers=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as response:
        return response.status, dict(response.headers), json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    name = "aceest-check-" + uuid.uuid4().hex[:10]
    containers = []
    network_created = volume_created = False
    with tempfile.TemporaryDirectory(prefix="aceest-router-") as directory:
        try:
            docker("network", "create", name)
            network_created = True
            docker("volume", "create", name)
            volume_created = True
            for slot, version in (
                ("stable", "1.0.0"),
                ("candidate", "2.0.0"),
                ("rolling", "2.0.0"),
            ):
                container = f"{name}-{slot}"
                docker(
                    "run",
                    "-d",
                    "--name",
                    container,
                    "--network",
                    name,
                    "--network-alias",
                    f"aceest-{slot}",
                    "--network-alias",
                    f"aceest-{slot}.aceest.svc.cluster.local",
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,mode=0700,uid=10001,gid=10001",
                    "-v",
                    f"{name}:/app/instance",
                    "-e",
                    f"APP_VERSION={version}",
                    "-e",
                    f"DEPLOYMENT_SLOT={slot}",
                    args.image,
                )
                containers.append(container)
            for container in containers:
                for attempt in range(30):
                    health = docker(
                        "inspect", "--format", "{{.State.Health.Status}}", container
                    )
                    if health == "healthy":
                        break
                    if attempt == 29:
                        raise RuntimeError(f"{container} never became healthy")
                    time.sleep(1)
            for percentage in (0, 10, 50, 100):
                config = nginx_config(percentage).replace(
                    "kube-dns.kube-system.svc.cluster.local", "127.0.0.11"
                )
                config_path = Path(directory) / "default.conf"
                config_path.write_text(config)
                router = f"{name}-router-{percentage}"
                docker(
                    "run",
                    "-d",
                    "--name",
                    router,
                    "--network",
                    name,
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,noexec,mode=0700,uid=101,gid=101",
                    "-p",
                    "127.0.0.1::8080",
                    "-v",
                    f"{config_path}:/etc/nginx/conf.d/default.conf:ro",
                    "nginxinc/nginx-unprivileged:1.28-alpine",
                )
                containers.append(router)
                port = json.loads(docker("inspect", router))[0]["NetworkSettings"][
                    "Ports"
                ]["8080/tcp"][0]["HostPort"]
                base = f"http://127.0.0.1:{port}"
                for attempt in range(30):
                    try:
                        request(base, "/health")
                        break
                    except OSError:
                        if attempt == 29:
                            raise
                        time.sleep(1)
                counts = collections.Counter(
                    request(base, "/canary/api/version")[2]["slot"] for _ in range(200)
                )
                if percentage == 0:
                    assert counts == {"stable": 200}, counts
                elif percentage == 100:
                    assert counts == {"candidate": 200}, counts
                else:
                    assert counts["stable"] and counts["candidate"], counts
                print(f"PASS canary {percentage}%: {dict(counts)}", flush=True)
                if percentage == 0:
                    assert (
                        request(base, "/rolling/api/version")[2]["version"] == "2.0.0"
                    )
                    assert (
                        request(base, "/blue-green/api/version")[2]["version"]
                        == "1.0.0"
                    )
                    for variant, version in (("A", "1.0.0"), ("B", "2.0.0")):
                        for _ in range(10):
                            _, headers, result = request(
                                base,
                                "/ab/api/version",
                                headers={"Cookie": f"aceest_variant={variant}"},
                            )
                            assert result["version"] == version
                            assert headers["X-ACEest-Variant"] == variant
                    _, headers, _ = request(base, "/ab/api/version")
                    assert "aceest_variant=" in headers["Set-Cookie"]
                    assert (
                        request(base, "/shadow/api/version?evidence=read")[2]["version"]
                        == "1.0.0"
                    )
                    request(
                        base,
                        "/shadow/api/members?evidence=write",
                        {
                            "name": "Shadow Test",
                            "email": "shadow@example.com",
                            "membership": "Basic",
                        },
                    )
                    time.sleep(1)
                    logs = docker("logs", f"{name}-candidate")
                    assert "GET /api/version?evidence=read" in logs, logs
                    assert "POST /api/members?evidence=write" not in logs, logs

                    def register(index, endpoint=base):
                        prefix = "/ab" if index % 2 else "/blue-green"
                        return request(
                            endpoint,
                            f"{prefix}/api/members",
                            {
                                "name": f"Shared Member {index}",
                                "email": f"shared{index}@example.com",
                                "membership": "Elite",
                            },
                        )[0]

                    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                        assert all(
                            status == 201 for status in pool.map(register, range(20))
                        )
                    assert len(request(base, "/rolling/api/members")[2]) == 21
                    print(
                        "PASS rolling, blue-green, sticky A/B, "
                        "read-only shadow, shared data",
                        flush=True,
                    )
                docker("rm", "-f", router)
                containers.remove(router)
            print("All routing integration checks passed.", flush=True)
        finally:
            for container in reversed(containers):
                docker("rm", "-f", container)
            if volume_created:
                docker("volume", "rm", name)
            if network_created:
                docker("network", "rm", name)


if __name__ == "__main__":
    main()

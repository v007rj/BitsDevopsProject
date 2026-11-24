"""Run a disposable local SonarQube server and save genuine analysis evidence."""

import base64
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.client import HTTPException

from scripts.routing import ROOT
from scripts.sonar_report import collect


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def api(base, path, password, data):
    request = urllib.request.Request(
        base + "/api/" + path, data=urllib.parse.urlencode(data).encode()
    )
    auth = base64.b64encode(f"admin:{password}".encode()).decode()
    request.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode()
        raise RuntimeError(f"SonarQube {path}: HTTP {error.code}: {detail}") from error


def main():
    name = "aceest-sonar-" + uuid.uuid4().hex[:10]
    network_created = server_created = False
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    coverage = reports / "coverage.xml"
    if not coverage.exists():
        raise FileNotFoundError("Run containerized pytest with XML coverage first.")
    source = coverage.read_text()
    for root in ("/work", "/app"):
        source = source.replace(f"<source>{root}</source>", "<source>.</source>")
    coverage.write_text(source)
    try:
        docker("network", "create", name)
        network_created = True
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            name,
            "--network-alias",
            "sonarqube",
            "--memory",
            "3g",
            "-p",
            "127.0.0.1::9000",
            "-e",
            "SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true",
            "sonarqube:community",
        )
        server_created = True
        port = json.loads(docker("inspect", name))[0]["NetworkSettings"]["Ports"][
            "9000/tcp"
        ][0]["HostPort"]
        base = f"http://127.0.0.1:{port}"
        for attempt in range(120):
            try:
                with urllib.request.urlopen(
                    base + "/api/system/status", timeout=5
                ) as response:
                    if json.load(response)["status"] == "UP":
                        break
            except (urllib.error.URLError, TimeoutError, HTTPException) as error:
                if attempt == 0:
                    print(f"Waiting for local SonarQube startup: {error}", flush=True)
            if attempt == 119:
                raise TimeoutError(
                    "SonarQube did not become ready within four minutes."
                )
            time.sleep(2)
        initial_password = os.getenv("SONAR_INITIAL_PASSWORD", "admin")
        password = "Aa1!" + secrets.token_urlsafe(20)
        api(
            base,
            "users/change_password",
            initial_password,
            {
                "login": "admin",
                "previousPassword": initial_password,
                "password": password,
            },
        )
        api(
            base,
            "projects/create",
            password,
            {
                "project": "aceest-fitness",
                "name": "ACEest Fitness and Gym",
            },
        )
        gate_name = "ACEest CI"
        api(base, "qualitygates/create", password, {"name": gate_name})
        for metric, operator, threshold in (
            ("coverage", "LT", "90"),
            ("reliability_rating", "GT", "1"),
            ("security_rating", "GT", "1"),
            ("duplicated_lines_density", "GT", "3"),
        ):
            api(
                base,
                "qualitygates/create_condition",
                password,
                {
                    "gateName": gate_name,
                    "metric": metric,
                    "op": operator,
                    "error": threshold,
                },
            )
        api(
            base,
            "qualitygates/select",
            password,
            {
                "projectKey": "aceest-fitness",
                "gateName": gate_name,
            },
        )
        token = api(base, "user_tokens/generate", password, {"name": name})["token"]
        scanner_env = os.environ.copy()
        scanner_env["SONAR_TOKEN"] = token
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0",
                "--network",
                name,
                "-e",
                "SONAR_TOKEN",
                "-e",
                "SONAR_HOST_URL=http://sonarqube:9000",
                "-v",
                f"{ROOT}:/usr/src",
                "sonarsource/sonar-scanner-cli:latest",
                "-Dsonar.projectVersion=2.0.0",
                "-Dsonar.scm.disabled=true",
                "-Dsonar.working.directory=/usr/src/.scannerwork",
            ],
            env=scanner_env,
            check=True,
        )
        properties = dict(
            line.split("=", 1)
            for line in (ROOT / ".scannerwork" / "report-task.txt")
            .read_text()
            .splitlines()
            if "=" in line
        )
        result = collect(base, token, properties["ceTaskId"])
        result["validationScope"] = (
            "Local working tree; disposable server, not hosted Jenkins."
        )
        (reports / "sonarqube.json").write_text(json.dumps(result, indent=2))
        print(f"SonarQube gate: {result['qualityGate']['status']}", flush=True)
        print(json.dumps(result["measures"], indent=2), flush=True)
        for issue in result["issues"]:
            print(
                f"{issue['type']} {issue['component']}:{issue.get('line', '?')}: "
                f"{issue['rule']} {issue['message']}",
                flush=True,
            )
        if result["qualityGate"]["status"] != "OK":
            raise SystemExit("Quality gate failed; see reports/sonarqube.json.")
    except (
        subprocess.CalledProcessError,
        OSError,
        ValueError,
        HTTPException,
        RuntimeError,
    ):
        if server_created:
            print(docker("logs", "--tail", "80", name))
        raise
    finally:
        if server_created:
            docker("rm", "-f", "-v", name)
        if network_created:
            docker("network", "rm", name)


if __name__ == "__main__":
    main()

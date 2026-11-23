"""Export genuine SonarQube analysis results; never substitute sample evidence."""

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def fetch(base_url, endpoint, token, parameters):
    query = urllib.parse.urlencode(parameters)
    request = urllib.request.Request(f"{base_url.rstrip('/')}/api/{endpoint}?{query}")
    auth = base64.b64encode(f"{token}:".encode()).decode()
    request.add_header("Authorization", f"Basic {auth}")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def collect(base_url, token, task_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = fetch(base_url, "ce/task", token, {"id": task_id})["task"]
        if task["status"] == "SUCCESS":
            break
        if task["status"] in ("FAILED", "CANCELED"):
            raise RuntimeError(f"SonarQube analysis {task_id}: {task['status']}")
        time.sleep(2)
    else:
        raise TimeoutError("SonarQube analysis did not finish before the deadline.")
    gate = fetch(
        base_url,
        "qualitygates/project_status",
        token,
        {"analysisId": task["analysisId"]},
    )["projectStatus"]
    metrics = fetch(
        base_url,
        "measures/component",
        token,
        {
            "component": task["componentKey"],
            "metricKeys": "coverage,bugs,vulnerabilities,code_smells,"
            "duplicated_lines_density,ncloc",
        },
    )
    issues = fetch(
        base_url,
        "issues/search",
        token,
        {
            "componentKeys": task["componentKey"],
            "resolved": "false",
            "ps": 100,
        },
    )
    return {
        "server": base_url,
        "task": task,
        "qualityGate": gate,
        "measures": metrics["component"]["measures"],
        "issues": issues["issues"],
        "issueTotal": issues["total"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--task-file", type=Path, default=Path(".scannerwork/report-task.txt")
    )
    parser.add_argument("--output", type=Path, default=Path("reports/sonarqube.json"))
    args = parser.parse_args()
    properties = dict(
        line.split("=", 1)
        for line in args.task_file.read_text().splitlines()
        if "=" in line
    )
    result = collect(
        os.environ["SONAR_HOST_URL"], os.environ["SONAR_TOKEN"], properties["ceTaskId"]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(f"SonarQube quality gate: {result['qualityGate']['status']}")
    if result["qualityGate"]["status"] != "OK":
        raise SystemExit("SonarQube quality gate failed; publishing is blocked.")
    if not result["qualityGate"]["conditions"]:
        raise SystemExit(
            "SonarQube gate evaluated no conditions; configure an overall gate."
        )


if __name__ == "__main__":
    main()

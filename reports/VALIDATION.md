# Assignment 2 validation

Validated locally on 23 November 2026:

- Docker release image builds successfully on Linux/Python 3.13.
- Ruff checks pass.
- **43 Pytest cases pass**; application branch-inclusive coverage **93.63%**.
- Real NGINX/Docker integration passes all five route modes.
- Latest 200-request canary checks with fully qualified DNS aliases:
  0% = 200 stable; 10% = 180 stable/20 candidate;
  50% = 96 stable/104 candidate; 100% = 200 candidate.
- A/B cookie routing is sticky; shadow reads reach candidate, tested writes do
  not; 20 concurrent registrations succeed and remain visible across versions.
- Focused unit tests verify rollback restores prior images/routing and refuses
  a different Kubernetes context.

Raw JUnit/coverage outputs are generated in this directory and archived by
Jenkins. [SonarQube analysis](SONARQUBE.md) describes genuine local scan evidence.
[The three-page report](../REPORT.html) is print-ready.
The strict Sonar gate is **blocked by the inherited CSRF finding**, intentionally
preserved for manual review at the owner's request. No findings were suppressed.

## Not yet verified or published

- Hosted Jenkins executions and webhook deliveries.
- A live Kubernetes rollout, outage measurement, and rollback demonstration.
- Public running-cluster URL.
- Owner-controlled public registry images for every version.

No Kubernetes context was configured. Do not interpret Docker routing tests or
mocked rollback unit tests as a successful Kubernetes deployment.
Use [the setup guide](../README.md) to complete these operational requirements.

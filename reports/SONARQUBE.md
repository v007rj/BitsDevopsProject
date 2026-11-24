# Genuine SonarQube analysis

Local scan: **23 November 2026**, SonarQube Community Build **26.11.0.134175**,
SonarScanner CLI **8.4.0.16527**, Python profile **Sonar way core**.

Raw, token-free server evidence: [sonarqube.json](sonarqube.json).
The raw report includes the authoritative task and analysis IDs.

| Measure | Result |
|---|---|
| Application coverage | 93.6% |
| Bugs | 0 |
| Vulnerabilities reported by this profile | 1 |
| Code smells | 1 |
| Duplicated lines | 0.0% |
| Quality gate | **ERROR - release blocked** |

The **ACEest CI** gate explicitly checks overall coverage >=90%, reliability
rating A, security rating A, and duplication <=3%. A first-project
new-code-only gate initially returned OK without checking any conditions; that
was rejected as insufficient evidence. The exporter now also blocks such
empty gates.

## Remaining inherited findings

- `python:S4502`, [app.py](../app.py): Flask does not enable CSRF protection.
- `python:S8786`, [app.py](../app.py): inherited email regex has a backtracking
  performance warning.

The owner explicitly chose to preserve the Assignment 1 API and document the
blocked gate for manual review rather than change the client/token contract.
No finding was suppressed or automatically marked safe. A reviewer must assess
the educational unauthenticated JSON API and record a justified disposition, or
implement protection with coordinated dashboard, API and test changes before
accepting a production release.

New-code findings concerning writable temporary mounts and HTTP probe transport
were addressed: integration containers use owner-only, noexec/nosuid/nodev
temporary mounts; smoke tests use kubectl's authenticated API-server Service
proxy. A subsequent genuine scan reduced reported vulnerabilities from five to
the single inherited CSRF finding.

The disposable local server was removed after exporting the results. Its URL in
the JSON is **not a live hosted dashboard**. Jenkins will generate fresh evidence
against the owner's installed SonarQube server, and will correctly refuse to
publish/deploy until the configured gate passes.

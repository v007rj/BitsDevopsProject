# ACEest Fitness & Gym - Assignment 2

Extends the existing [Assignment 1 application](app.py) rather than replacing it.
Repository: <https://github.com/vinayakmsft/BitsDevopsProject>.

## Architecture and deliverables

```text
GitHub push / PR -> Jenkins checkout -> Docker build -> Ruff + container Pytest
   -> SonarQube analysis -> quality gate -> Docker Hub immutable image
   -> Kubernetes deployment -> readiness + route smoke tests -> success / rollback

Client -> NGINX router -> blue / green / candidate / rolling Gunicorn + Flask
                                  -> shared SQLite PVC (single Minikube node)
```

| Deliverable | Location |
|---|---|
| Assignment-compatible Flask entry point | [ACEest_Fitness.py](ACEest_Fitness.py) |
| Existing dashboard, members and workouts implementation | [app.py](app.py), [templates](templates), [static](static) |
| Incremental releases | [VERSION](VERSION), [versions.json](versions.json), Git history |
| CI/CD and image | [Jenkinsfile](Jenkinsfile), [Dockerfile](Dockerfile) |
| Kubernetes infrastructure | [k8s](k8s), [deployment driver](scripts/deploy.py) |
| Tests and genuine analysis output | [tests](tests), [reports](reports) |
| Verified three-page report | [REPORT.pdf](REPORT.pdf), [editable HTML](REPORT.html) |

**Evidence boundary:** local tests, routing checks and analysis are distinct from
hosted Jenkins executions, registry publishing and a publicly reachable cluster.
See [validation status](reports/VALIDATION.md). Do not submit placeholder URLs or
claim a Jenkins run based only on the local checks.

## Run the existing application

PowerShell, from this project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe ACEest_Fitness.py
```

Open <http://localhost:5000>. `app.py` remains a compatible entry point.

| Method | Path | Function |
|---|---|---|
| GET | `/` | Dashboard |
| GET | `/health` | Database-aware readiness |
| GET | `/live` | Process liveness, independent of the database |
| GET | `/api/version` | Release version and deployment slot |
| GET, POST | `/api/members` | List / register members |
| DELETE | `/api/members/<id>` | Remove a member and their workouts |
| GET, POST | `/api/workouts` | List / record workouts |

JSON member fields: `name`, `email`, `membership` (`Basic`, `Premium`, `Elite`),
optional ISO `joined_on`. Workout fields: `member_id`, `workout_type` (`Cardio`,
`Strength`, `Flexibility`, `HIIT`, `Recovery`), `duration_minutes` (1-1440),
`calories_burned` (non-negative), optional ISO `completed_on`.
`DATABASE_PATH` overrides the local SQLite path; `APP_VERSION` and
`DEPLOYMENT_SLOT` identify the running release. Preserve the default database
path or mount `/app/instance` for persistence.

## Containerized validation

```powershell
docker build --build-arg APP_VERSION=2.0.0 -t aceest-fitness:local .
docker run --rm aceest-fitness:local ruff check .
docker run --rm aceest-fitness:local pytest
New-Item -ItemType Directory -Force reports | Out-Null
docker run --rm --user 0 -e DATABASE_PATH=/tmp/test.sqlite `
  -v "${PWD}\reports:/reports" aceest-fitness:local `
  pytest --junitxml=/reports/pytest.xml --cov-report=xml:/reports/coverage.xml `
  --cov-report=json:/reports/coverage.json
python -m scripts.check_router --image aceest-fitness:local
```

The last command launches only uniquely named temporary containers, a network
and a volume, checks all routing modes and concurrent shared-data writes, and
removes those resources. It does not touch other Docker workloads.
The runtime is UID/GID 10001; a writable report bind mount may require the
explicit UID override shown above. Jenkins instead uses its agent UID/GID.
Coverage must be at least **90% including branches for the Flask application**.
Infrastructure scripts have focused unit tests and routing integration checks,
but are excluded from Sonar's application coverage calculation.

```powershell
docker compose up --build -d
docker compose down
```

Compose retains its named database volume. Never delete it unless you intend
to discard member data.

## Jenkins, SonarQube and Docker Hub setup

1. Provision a Linux Jenkins controller and a Linux agent labelled
   **aceest-linux** with Git, Docker, Python 3 and kubectl. Native Windows
   Jenkins agents are not supported by this shell-based pipeline. An agent
   must have authorized Docker access; never expose an unauthenticated daemon.
2. Install **Pipeline**, **Git**, **GitHub Branch Source**, **JUnit**,
   **Credentials Binding**, and **SonarQube Scanner for Jenkins** plugins.
   Under **Tools**, install a CLI scanner named **SonarScanner**.
3. Start a SonarQube Community installation with durable storage; an educational
   local instance can run `docker run -d --name aceest-sonarqube -p 127.0.0.1:9000:9000
   -e SONAR_ES_BOOTSTRAP_CHECKS_DISABLE=true sonarqube:community`.
   This disables Elasticsearch bootstrap checks **only for a local lab**.
   Change the initial admin password immediately. Use a supported external
   PostgreSQL database and normal bootstrap checks for a durable installation.
4. In SonarQube create project **aceest-fitness**, generate a project analysis
   token, and assign an **ACEest CI** custom gate with **overall-code** conditions:
   coverage >=90%, reliability rating A, security rating A, and duplication <=3%.
   Keep the recommended new-code conditions as well if desired. Overall
   conditions are essential: a new project's new-code-only gate can return
   "OK" without evaluating any conditions. The exporter rejects that empty
   result. A failed gate always blocks publishing/deployment.
5. Jenkins **Credentials**:
   - `sonarqube-token`: Secret Text project token.
   - `dockerhub`: Username + Docker Hub access token (not committed to Git).
   - `aceest-kubeconfig`: Secret File, a working kubeconfig with credentials and
     endpoints reachable **from the Linux agent**.
     Apply [the deployment Role](k8s/rbac.yaml) using an administrator during
     setup. The namespace-scoped account supports workload changes, pod exec
     for canary sampling, and `services/proxy` for authenticated smoke checks;
     it cannot bootstrap namespaces or access Secrets.
6. **System > SonarQube servers**: name **aceest-sonarqube**, URL reachable from
   the agent, credential `sonarqube-token`. Add SonarQube webhook
   `https://JENKINS_HOST/sonarqube-webhook/` (trailing slash required).
   If services run on different container networks, `localhost` is not a
   shared host. Confirm connectivity in both directions.
7. Create a **Multibranch Pipeline**, use the repository above as a GitHub
   source, and scan it. Script path: `Jenkinsfile`. Add GitHub push webhook
   `https://JENKINS_HOST/github-webhook/`. SCM polling every five minutes is a
   fallback. Do not give registry/deployment credentials to untrusted PR jobs.
8. Create your own **public Docker Hub repository**. On the `main` branch job,
   set `IMAGE_REPOSITORY` to `YOUR_DOCKERHUB_USER/aceest-fitness`. Builds publish
   `VERSION-build.BUILD_NUMBER-COMMIT` tags only after both test and quality
   gates pass. Kubernetes uses the pushed **digest**, not a mutable `latest`.
   Set `DEPLOY=true` only after cluster bootstrap below.

Jenkins archives JUnit, XML/JSON coverage, genuine Sonar results, image digest,
deployment result and the previous-release rollback snapshot. Failed tests or
analysis never publish images. `DEPLOY=false` intentionally enables a
quality-gated build/publish without altering infrastructure.

For a **disposable local analysis** without configuring Jenkins:

```powershell
python -m scripts.local_sonar
```

Run after creating XML coverage. This pulls SonarQube and its CLI, starts a
server bound only to localhost, changes its default password, scans locally,
exports `reports\sonarqube.json`, then deletes its own server and network.
The report is genuine evidence but its temporary dashboard URL expires.
No code is sent to a third-party analysis service.
**Current expected result:** the strict gate exits nonzero after saving evidence
because the inherited CSRF finding remains open. The owner chose to preserve the
existing API and leave that finding for review. See [the scan report](reports/SONARQUBE.md);
do not suppress the gate simply to obtain a green build.

## Minikube bootstrap (single-node lab)

Install Minikube and kubectl using the supported Windows installation
instructions. Docker Desktop must be running. Commands below operate on
**your Minikube context**, never the currently selected context implicitly.

```powershell
minikube start --driver=docker --cpus=4 --memory=4096
kubectl --context minikube label node minikube aceest-storage=true --overwrite
docker build --build-arg APP_VERSION=2.0.0 -t aceest-fitness:2.0.0 .
minikube image load aceest-fitness:2.0.0
python -m scripts.deploy render --context minikube `
  --image aceest-fitness:2.0.0 --version 2.0.0
python -m scripts.deploy bootstrap --context minikube `
  --image aceest-fitness:2.0.0 --version 2.0.0
kubectl --context minikube -n aceest get deployments,pods,services,pvc
minikube service aceest-router -n aceest --url
```

The PVC uses Minikube's default storage class. `bootstrap` deliberately refuses
to overwrite an existing baseline. If initial bootstrap fails, inspect events
and repair the cause before retrying; there is no previous stable release to
restore on a brand-new cluster. Do not manually reset a populated namespace.

On Windows Docker-driver Minikube, keep the `minikube service --url` terminal
open. Alternatively:

```powershell
kubectl --context minikube -n aceest port-forward service/aceest-router 8080:80
```

The forwarding process must stay running. `http://127.0.0.1:8080` is local,
**not a publicly accessible submission endpoint**. For remote review, use an
authorized HTTPS ingress/load balancer or an approved tunnel, and supply the
actual reachable URL. Configure TLS and authentication before exposing real
member data; the inherited educational app has no authentication.

### Shared-data constraint

All eight application replicas use one local SQLite PVC and a 30-second lock
timeout. The `aceest-storage=true` label **must be assigned to exactly one
node**, colocating all replicas on the same local filesystem. This preserves
members/workouts across colors, canary, A/B and rolling pods without duplicating
data. This is an intentional Minikube demo, **not multi-node HA**. Do not put
SQLite on NFS or distribute its file across nodes. For a cloud deployment,
migrate to managed PostgreSQL first, remove node colocation, and use
backward-compatible expand/contract migrations; application rollback cannot
undo destructive database migrations or committed user writes.

## Five deployment strategies and rollback

All API paths are available through one router; prefixes are removed before
proxying to Flask. For example `/ab/api/version`, not just `/ab`.
Use the root `/` for the dashboard: inherited absolute UI asset/API links are
not designed for a dashboard mounted below these strategy prefixes.

| Strategy | Endpoint prefix | Actual behavior |
|---|---|---|
| Rolling | `/rolling/` | Two replicas, maxUnavailable=0, maxSurge=1, readiness-gated update |
| Blue-green | `/blue-green/` and `/` | Update inactive color, verify, switch stable Service selector; keep old color |
| Canary | `/canary/` | Per-request 10% -> 50% -> 100%, 200-request checks and pause at each stage; promote stable then reset canary to 0% |
| Shadow | `/shadow/` | Respond from stable; mirror GET/HEAD only to candidate; never replay POST/DELETE writes |
| A/B | `/ab/` | 50/50 initial assignment, sticky `aceest_variant=A/B` cookie; candidate remains isolated from stable routing |

Shadow response bodies are discarded, so inspect candidate and router logs to
confirm mirrored traffic. A/B is a traffic-routing demonstration, not a
statistically powered business experiment. Do not promote it based solely on
HTTP availability. Both A/B and shadow deliberately keep stable unchanged.
Canary percentages are probabilistic, not exact quotas per 200 requests.
Readiness and successful smoke checks establish application-level availability,
not a universal zero-downtime guarantee during a node failure.

Build/load another version, then run (or let Jenkins run after registry push):

```powershell
python -m scripts.deploy deploy --context minikube `
  --image YOUR_DOCKERHUB_USER/aceest-fitness:IMMUTABLE_TAG `
  --version 2.1.0 --strategy blue-green --evidence artifacts
# Repeat with rolling, canary, shadow or ab.
python -m scripts.deploy rollback --context minikube `
  --rollback-file .\artifacts\rollback.json
```

Each deployment persists previous Deployment specs, router config reference,
and stable Service selector **before** making changes. Failure restores all
saved objects, waits for readiness and verifies the previous stable version,
then exits nonzero. A rollback failure also remains an explicit failure.
Download Jenkins' `artifacts/rollback.json` to perform the same rollback later.
Immutable routing ConfigMaps are retained for rollback; do not prune configs
referenced by retained ReplicaSets. Run only one release driver against this
namespace at a time; Jenkins job-level concurrency locking does not serialize
manual operations or separate Jenkins jobs.

## Versions, collaboration and final submission

Assignment 1 remains available at commit `0cfe471`; do not copy divergent
application files just to fabricate versions. Create annotated release tags
after real commits, update `VERSION` for new releases and use PRs with required
GitHub Actions and Jenkins checks. The existing Actions workflow remains a
fast build/lint/container-test check; Jenkins performs delivery.

Publish the original application too (run in a separate worktree):

```powershell
git worktree add ..\aceest-v1 0cfe471
docker build -t YOUR_DOCKERHUB_USER/aceest-fitness:1.0.0 ..\aceest-v1
docker push YOUR_DOCKERHUB_USER/aceest-fitness:1.0.0
git worktree remove ..\aceest-v1
# After committing/merging Assignment 2:
git tag -a v2.0.0 -m "Assignment 2 CI/CD release"
git push origin main --tags
```

The registry commands require your own authenticated Docker Hub account.
Never paste passwords/tokens into files or command-line arguments.

Before submitting:

- Merge the implementation PR and verify the public repository contents.
- Supply the real running-cluster URL and demonstrate all five prefixes.
- Attach successful Jenkins runs for recent commits, not local substitutes.
- Attach SonarQube dashboard evidence and the exported analysis report.
- Resolve or formally review the inherited CSRF finding before expecting the
  strict Jenkins release gate to pass.
- Link your own public image repository containing every release.
- Print [REPORT.html](REPORT.html) to A4/PDF; update evidence for the final run.

## Troubleshooting

- Pending PVC/pods: check Minikube's default storage class, the single labelled
  storage node, and `kubectl --context minikube -n aceest get events`.
- Rollout failure: inspect pod logs and image pull errors. Private images need
  `imagePullSecrets` configured before use; this lab expects public images.
- Jenkins cannot reach Minikube: Docker-driver host-only addresses or Windows
  kubeconfig file paths are not portable to a remote Linux agent. Use a
  reachable cluster endpoint and self-contained credential files.
- Sonar gate/webhook timeout: confirm the project token, scanner tool name,
  configured server URL and trailing-slash webhook reachability.

Educational use. The existing unauthenticated application is not a production
member-management service.

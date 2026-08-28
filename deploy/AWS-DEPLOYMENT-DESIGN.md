# Business Context Layer (BCL) — AWS Deployment Design

Deployment design for running the `agenticdatalayer` demo (Business Context Layer) in
**our AWS environment**.

- **Account:** `054663422011`
- **Region:** `us-east-1`
- **Identity used for deploys:** `iam:user/AlexeyF_CLI` (MFA session via `connect.py`)
- **Status:** greenfield for containers (0 ECS clusters today; existing `tcc-app` uses ALB→Lambda)
- **Tagging rule:** every asset created for this workload carries `Project = BCL` (see [§7](#7-bcl-tagging-standard)).

> This is a design, not an applied change. No infrastructure is provisioned by this
> document. Provisioning is a separate, confirmed step.

---

## 1. What we are deploying

Nine FastAPI/uvicorn Python services, grouped into three architectural layers plus a
cross-cutting logger. Today they run on one host via `start.sh`, talk to each other over
**hardcoded `http://localhost:<port>`**, and hold **all state in memory** (seeded from YAML).

| Port | Service | Layer | Public? | Calls (downstream) |
|------|---------|-------|---------|--------------------|
| 8013 | ACG + Agent UI | Agentic | **Yes** (UI + MCP SSE + read API) | Cache, Offer Engine, Log |
| 8018 | Action Broker | Agentic | **Yes** (write API, JWT-gated) | Billing, Catalogue, Log |
| 8011 | CDC Assembly | Business Context | No | Cache, Catalogue, Log |
| 8012 | Context Cache | Business Context | No | Log |
| 8016 | Offer Engine | Business Context | No | Catalogue |
| 8010 | CRM SoR | System of Record | No | CDC, Log |
| 8014 | Product Catalogue SoR | System of Record | No | CDC, Log |
| 8017 | Billing SoR | System of Record | No | CDC, Log, Catalogue |
| 8015 | Logging | Cross-cutting | No | — |

**Runtime:** Python 3.12, `fastapi`, `uvicorn`, `httpx`, `pydantic v2`, `mcp` (SSE),
`pydantic-graph`, `networkx`, `pyyaml`, `PyJWT`.

---

## 2. Platform choice: ECS Fargate (not Lambda)

The existing `tcc-app` uses ALB→Lambda, but that pattern does **not** fit this workload:

- **Always-on & interdependent** — 9 services that call each other continuously; Lambda cold
  starts and per-invoke fan-out would be slow and awkward.
- **MCP SSE (ACG)** — the Model Context Protocol server holds **long-lived streaming
  connections**. Lambda's request/response model and timeouts are a poor fit for SSE.
- **Stateful in-memory stores** — cache tiers, pending-join store, audit deque assume a
  persistent process.

**Decision:** **Amazon ECS on Fargate** — one ECS **service per component**, each a
long-running container, in a private VPC, fronted by ALBs. Serverless containers (no EC2 to
manage) keep ops light while giving us persistent processes and SSE support.

---

## 3. Target architecture

```
                       Internet
                          │  HTTPS (ACM cert, custom domain)
                 ┌────────▼─────────┐
                 │  Public ALB      │   (BCL-public-alb)
                 │  /            → ACG (8013)          [UI + MCP SSE]
                 │  /actions/*   → Action Broker (8018)[write API]
                 └───┬───────────┬──┘
   ┌─────────────────┘           └───────────────────┐
   │  VPC  BCL-vpc (us-east-1, 2 AZs)                 │
   │  ┌────────── public subnets ──────────┐          │
   │  │  Public ALB + NAT GW               │          │
   │  └────────────────────────────────────┘          │
   │  ┌────────── private subnets ─────────────────────┐
   │  │  ECS Fargate cluster: BCL-cluster              │
   │  │                                                │
   │  │  Agentic:   acg(8013)      action-broker(8018) │
   │  │  Business:  cdc(8011) cache(8012) offer(8016)  │
   │  │  SoR:       crm(8010) catalogue(8014) billing(8017)
   │  │  Cross:     log(8015)                          │
   │  │                                                │
   │  │  Service-to-service via AWS Cloud Map          │
   │  │  private DNS:  <svc>.bcl.local                 │
   │  └────────────────────────────────────────────────┘
   └───────────────────────────────────────────────────┘
```

- **Public ALB** exposes only the two agentic entry points (ACG, Action Broker). Everything
  else is private.
- **Internal service discovery** via **AWS Cloud Map** private namespace `bcl.local`
  (e.g. `cache.bcl.local:8012`). This replaces the hardcoded `localhost` URLs.
- **Cross-service traffic** stays inside the VPC on the service security group.

---

## 4. Per-service mapping

Each service → 1 ECS Fargate service + 1 task definition + 1 ECR repo + 1 Cloud Map entry.
Sizing is a starting point (demo scale); tune later.

| Service | ECS service | ECR repo | vCPU / Mem | Desired count | Discovery name | Ingress |
|---------|-------------|----------|-----------|---------------|----------------|---------|
| ACG | `bcl-acg` | `bcl/acg` | 0.5 / 1 GB | 1 | `acg.bcl.local` | Public ALB `/` |
| Action Broker | `bcl-action-broker` | `bcl/action-broker` | 0.25 / 0.5 GB | 1 | `action-broker.bcl.local` | Public ALB `/actions/*` |
| CDC Assembly | `bcl-cdc` | `bcl/cdc` | 0.25 / 0.5 GB | 1 | `cdc.bcl.local` | Internal |
| Context Cache | `bcl-context-cache` | `bcl/context-cache` | 0.5 / 1 GB | 1 | `cache.bcl.local` | Internal |
| Offer Engine | `bcl-offer-engine` | `bcl/offer-engine` | 0.25 / 0.5 GB | 1 | `offer.bcl.local` | Internal |
| CRM SoR | `bcl-crm` | `bcl/crm` | 0.25 / 0.5 GB | 1 | `crm.bcl.local` | Internal |
| Product Catalogue | `bcl-catalogue` | `bcl/catalogue` | 0.25 / 0.5 GB | 1 | `catalogue.bcl.local` | Internal |
| Billing SoR | `bcl-billing` | `bcl/billing` | 0.25 / 0.5 GB | 1 | `billing.bcl.local` | Internal |
| Logging | `bcl-log` | `bcl/log` | 0.25 / 0.5 GB | 1 | `log.bcl.local` | Internal |

Each task: 1 container, health check `GET /` or `/health` (ACG/Action Broker via ALB target
group; internal services via ECS container health check), logs to CloudWatch.

---

## 5. Required code changes (small, backward-compatible)

The demo is close to container-ready. The essential change is **externalising the hardcoded
service URLs** so they resolve to Cloud Map DNS in AWS and still default to `localhost` locally.

1. **Service URLs → env vars.** Replace each `X_URL = "http://localhost:PORT"` with
   `os.getenv("X_URL", "http://localhost:PORT")`. Files: `acg`, `action_broker`, `cdc`,
   `context_cache`, `offer_engine`, `crm`, `product`, `billing` apps. (Defaults preserve the
   current local `start.sh` flow unchanged.)
2. **Dockerfile per service** (shared base). One slim `python:3.12-slim` image, install
   `requirements.txt`, copy the repo (services import shared `ontology`/`rules`/`auth` via
   `PYTHONPATH`), then `CMD uvicorn <app>:app --host 0.0.0.0 --port <port>`. Parameterise the
   app module + port via build args or a common entrypoint.
3. **JWT secret → Secrets Manager.** `auth.py` currently hardcodes `demo-secret-not-for-
   production`. Inject via env from a `BCL/jwt-signing-secret` secret. (Longer term: move to
   RS256/asymmetric issued by a real auth/Cognito.)
4. **Seed data** stays baked into the image (YAML files) for the demo; no change needed.

No business-logic changes. The estimation/assembly/graph logic is untouched.

---

## 6. State, security, networking, observability

- **State (in-memory today):** acceptable for a demo — a task restart re-seeds from YAML via
  the startup events. If we need durability later:
  - Context Cache → **DynamoDB** (Tier-1 permanent store) + optional **ElastiCache** hot tier.
  - Action Broker audit log → **DynamoDB** (append-only) or CloudWatch Logs.
  - CDC pending-join store → **DynamoDB** with TTL (mirrors the 30-second timeout).
- **Auth:** JWT (HS256 demo) → secret in **Secrets Manager**; Action Broker keeps issuing
  tokens at `/token`. ALB can optionally enforce OIDC in front for the UI.
- **Networking:** VPC with 2 AZs, public subnets (ALB + NAT), private subnets (Fargate tasks).
  Security groups: public ALB SG → ACG/Action Broker task SG; a shared internal SG allows
  service-to-service on 8010–8018. No public IPs on tasks.
- **Secrets/registry:** ECR repos per service; images scanned on push.
- **Observability:** each service already posts to the in-repo **Logging service (8015)** —
  keep it for the demo's audit trail, and *also* ship container stdout to **CloudWatch Logs**
  (`/bcl/<service>`). Add Container Insights on the cluster.
- **TLS/DNS:** ACM cert on the public ALB; a subdomain under the existing `data-ai-aws.com`
  zone (e.g. `bcl.data-ai-aws.com`).

---

## 7. BCL tagging standard

**Every** resource created for this workload MUST carry the base tag `Project = BCL`, plus the
common set below. Apply consistently across ECS, ECR, ALB/TargetGroups, VPC/subnets/SGs,
Cloud Map, Secrets Manager, DynamoDB (if used), CloudWatch log groups, and IAM roles.

| Tag key | Value | Purpose |
|---------|-------|---------|
| `Project` | `BCL` | **Mandatory** — identifies all Business Context Layer assets |
| `Component` | e.g. `acg`, `cdc`, `context-cache`, `action-broker`, `network`, `shared` | Which service/piece |
| `Layer` | `agentic` \| `business-context` \| `system-of-record` \| `cross-cutting` \| `platform` | Architectural layer |
| `Environment` | `demo` (later `dev`/`prod`) | Lifecycle stage |
| `ManagedBy` | `terraform` (or `cli`) | Provenance |
| `Owner` | `AlexeyF` | Accountability |

**How the mandatory `Project=BCL` tag gets applied automatically:**

- **Terraform:** set provider-level default tags so *nothing* can be created untagged:
  ```hcl
  provider "aws" {
    region = "us-east-1"
    default_tags {
      tags = {
        Project     = "BCL"
        Environment = "demo"
        ManagedBy   = "terraform"
        Owner       = "AlexeyF"
      }
    }
  }
  ```
  (Per-resource `tags` add `Component` / `Layer`.)
- **AWS CDK:** `Tags.of(app).add("Project", "BCL")` at the app root.
- **CLI/boto3:** pass `--tags Key=Project,Value=BCL ...` on every create call.

**Governance:** add a tag policy / config rule that flags any resource in this account missing
`Project=BCL`, so drift is caught. Cost is then trivially filterable by `Project=BCL` in Cost
Explorer.

---

## 8. Recommended repo/solution structure

```
agentic-data-layer/
├─ (upstream services: acg/ action_broker/ cdc/ ... unchanged logic)
├─ deploy/
│  ├─ AWS-DEPLOYMENT-DESIGN.md      ← this document
│  ├─ docker/
│  │  ├─ Dockerfile                 ← shared base image (app module + port as args)
│  │  └─ entrypoint.sh
│  ├─ terraform/                    ← IaC (default_tags = Project:BCL)
│  │  ├─ main.tf  providers.tf  vpc.tf  ecs.tf  alb.tf  cloudmap.tf
│  │  ├─ ecr.tf  secrets.tf  iam.tf  variables.tf  outputs.tf
│  │  └─ services.tf                ← the 9 ECS services from the §4 table
│  └─ scripts/
│     ├─ build-and-push.ps1         ← build 9 images → ECR (mirrors tcc-deploy pattern)
│     └─ deploy.ps1                 ← terraform apply + ECS service update
```

---

## 9. Phased rollout

1. **Containerise** — env-var URLs + shared Dockerfile; run all 9 via `docker compose` locally
   to confirm parity with `start.sh`.
2. **Foundation (IaC)** — VPC, ECS cluster, ECR repos, Cloud Map namespace, IAM task roles,
   Secrets Manager entry — all tagged `Project=BCL`.
3. **Deploy internal services** — log, cache, catalogue, offer, cdc, crm, billing (private).
4. **Deploy agentic tier** — ACG + Action Broker behind the public ALB (ACM + subdomain).
5. **Verify** — run the repo's `seed/seed.py`, `purchase_journey.py`, `pricing_journey.py`,
   and `tests/` against the deployed endpoints; confirm MCP SSE works through the ALB.
6. **Harden (optional)** — DynamoDB/ElastiCache for durable state, OIDC on the ALB, RS256 JWT.

---

## 10. Cost & risk notes

- **Cost:** ~9 always-on Fargate tasks at 0.25–0.5 vCPU + one ALB + NAT GW. This is the main
  cost driver versus the near-zero-idle Lambda app. For a demo, consider scaling non-critical
  services to `desiredCount=0` when idle, or collapsing SoRs into fewer tasks.
- **Risk:** in-memory state means a task restart drops runtime mutations (re-seeded from YAML).
  Fine for a demo; call out before any "production" use.
- **Security:** demo JWT secret and `*-not-for-production` auth must be replaced before real
  data. Keep everything private except the two agentic entry points.

---

### Open questions for sign-off
1. Confirm **ECS Fargate** (vs. a single-task all-in-one container, which is cheaper but less
   representative of the target architecture).
2. Environment name — proceed as `demo`?
3. Public exposure — is a public subdomain acceptable, or keep it internal-only (VPN/PrivateLink)?
4. Do we want durable state (DynamoDB) now, or keep in-memory for the first cut?

# BCL on AWS — as-built deployment

This folder documents how the Business Context Layer (BCL) demo is actually deployed to AWS,
and provides the scripts to reproduce it. It complements the design in
[`../deploy/AWS-DEPLOYMENT-DESIGN.md`](../deploy/AWS-DEPLOYMENT-DESIGN.md).

- **Account:** `054663422011` · **Region:** `us-east-1`
- **Deploy identity:** `iam:user/AlexeyF_CLI` (short-lived MFA session)
- **Tagging:** every asset carries `Project=BCL`
- **Live URLs:** see [`URLS.md`](./URLS.md)

## What is deployed

A single **all-in-one container** running all nine FastAPI services (matches the local
`start.sh` model — services talk over `localhost`). This was chosen over nine separate Fargate
services because an ALB supports a limited number of target groups and the demo only needs a
handful of externally reachable ports.

```
Internet ──▶ Application Load Balancer (bcl-alb)
              :80    ─▶ ACG (8013)   UI + read API + /chat (Bedrock)
              :8018  ─▶ Action Broker (8018)  token + write API
              :8010  ─▶ CRM SoR
              :8014  ─▶ Catalogue SoR
              :8017  ─▶ Billing SoR
                         │
                         ▼
              ECS Fargate service  bcl-svc  (cluster bcl-cluster)
              task def  bcl-all-in-one  (2 vCPU / 4 GB, assignPublicIp)
              task role bcl-task-role  ─▶  Amazon Bedrock (Converse)
```

Internal services (CDC 8011, Cache 8012, Log 8015, Offer 8016) are reachable only inside the
task on `localhost` and are not published through the ALB.

## Chat with data — Amazon Bedrock

The ACG exposes `POST /chat` (JWT-gated). It assembles the customer's governed context and
compatible offers through the same two-tier retrieval plan used by `/context`, grounds a
system prompt on it, and calls Bedrock via the **Converse API**.

- **Model:** `amazon.nova-lite-v1:0` (override with the `BEDROCK_MODEL` env var). Nova micro/lite
  are the invokable families in this account; Claude/Titan/Llama are not granted.
- **Credentials:** the container uses the `bcl-task-role` — no keys are baked into the image.
- **Fallback:** the UI calls `/chat` first and falls back to a deterministic answer engine if
  the LLM is unavailable, so the demo never dead-ends.

## Files

| File | Purpose |
|------|---------|
| `URLS.md` | All live endpoints + a verification snippet |
| `task-definition.json` | The registered `bcl-all-in-one` task definition (reference) |
| `iam/bcl-task-role-trust-policy.json` | Trust policy (ECS tasks assume role) |
| `iam/bcl-bedrock-invoke-policy.json` | Inline policy granting Bedrock invoke |
| `scripts/detect_bedrock_model.py` | Probe which Bedrock models are invokable (Converse) |
| `scripts/create_task_role.py` | Create `bcl-task-role` + register a task def revision |
| `scripts/deploy.ps1` | Build, push, re-assert SGs, roll the service, verify `/chat` |
| `scripts/deploy_finish.py` | SG re-assertion + ECS roll + ALB verification (called by deploy.ps1) |

## Reproduce a deployment

Prerequisites: Docker Desktop, AWS CLI, Python 3.12 with `boto3`, and a valid AWS session
written to `d:\code-ai-lab\.env` (via the MFA login skill).

```powershell
# 1. (optional) detect an invokable Bedrock model
python AWS/scripts/detect_bedrock_model.py

# 2. create the task role + register a task def revision that uses it
python AWS/scripts/create_task_role.py

# 3. build + push the image, re-assert SGs, roll the service, verify /chat
powershell -ExecutionPolicy Bypass -File AWS/scripts/deploy.ps1
```

## Operational notes

- **Security-group guardrail:** the account periodically strips `0.0.0.0/0` egress rules.
  `deploy_finish.py` re-asserts the full SG rule set on every deploy. If the site returns 504,
  check task-SG egress first.
- **Credentials expire hourly:** refresh the MFA session before each deploy (the deploy script
  does this automatically).
- **State is in-memory:** a task restart re-seeds from the YAML fixtures; runtime mutations are
  not durable. Fine for a demo — see the design doc for the DynamoDB/ElastiCache path.
- **Not production-hardened:** HTTP only (no TLS), demo JWT secret, Bedrock policy scoped to
  `Resource:"*"`. Tighten before using real data.

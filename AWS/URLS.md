# BCL on AWS — Live URLs

All endpoints for the Business Context Layer demo running on **Amazon ECS Fargate** behind an
Application Load Balancer in `us-east-1`. Every asset is tagged `Project=BCL`.

- **Account:** `054663422011`
- **Region:** `us-east-1`
- **Load balancer DNS:** `bcl-alb-131414283.us-east-1.elb.amazonaws.com`
- **Base URL:** http://bcl-alb-131414283.us-east-1.elb.amazonaws.com

> The demo is served over HTTP (no ACM cert / custom domain yet). All services run in a single
> all-in-one Fargate task; each service is published on its native port through the ALB.

---

## Primary entry point (Agent UI + ACG API) — port 80

The Agent Context Gateway serves the UI and its read API on the ALB's port 80 (container `:8013`).

| What | URL |
|------|-----|
| **Agent UI** (Explorer / Overview / Full Demo / Chat) | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/ |
| ACG OpenAPI docs | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/docs |
| Customer context | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/context/C001 |
| Compatible offers | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/compatible-offers/C001 |
| **Chat with data (Amazon Bedrock)** — `POST` | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/chat |
| Retrieval plan (graph) | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/retrieval-plan |
| Retrieval plan (Mermaid) | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/retrieval-plan/mermaid |
| Ontology / assembly spec | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/ontology |
| Cache status (both tiers) | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/cache-status |
| MCP SSE endpoint | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com/mcp/sse |

> `/chat`, `/context`, `/compatible-offers`, `/retrieval-plan`, `/ontology`, `/cache-status`
> require a Bearer JWT obtained from the Action Broker `/token` (see below).

## Action Broker (write API, JWT) — port 8018

| What | URL |
|------|-----|
| Broker docs | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com:8018/docs |
| Issue token — `POST {"client_id":"ui-client"}` | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com:8018/token |
| Submit intent — `POST` | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com:8018/submit-intent |
| Audit log — `GET` | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com:8018/audit?limit=20 |

## Systems of Record (read/docs) — ports 8010 / 8014 / 8017

| Service | Docs URL |
|---------|----------|
| CRM SoR | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com:8010/docs |
| Catalogue SoR | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com:8014/docs |
| Billing SoR | http://bcl-alb-131414283.us-east-1.elb.amazonaws.com:8017/docs |

## Internal-only (not exposed via the ALB)

Reachable only inside the container/VPC on `localhost`:

| Service | Port |
|---------|------|
| CDC Assembly | 8011 |
| Context Cache | 8012 |
| Logging | 8015 |
| Offer Engine | 8016 |

---

## Quick verification (PowerShell)

```powershell
$alb = 'http://bcl-alb-131414283.us-east-1.elb.amazonaws.com'
# 1. Get a token from the Action Broker
$tok = (Invoke-RestMethod -Method Post -Uri "$alb`:8018/token" `
        -ContentType 'application/json' -Body '{"client_id":"ui-client"}').access_token
# 2. Ask the Bedrock-backed chat a grounded question
Invoke-RestMethod -Method Post -Uri "$alb/chat" `
  -Headers @{ Authorization = "Bearer $tok" } -ContentType 'application/json' `
  -Body '{"customer_id":"C001","question":"What products does this customer hold?"}'
```

## AWS resources (console references)

| Resource | Name / ID |
|----------|-----------|
| ECS cluster | `bcl-cluster` |
| ECS service | `bcl-svc` |
| Task definition | `bcl-all-in-one` |
| Task role | `bcl-task-role` (bedrock invoke) |
| Execution role | `ecsTaskExecutionRole` |
| ECR repository | `054663422011.dkr.ecr.us-east-1.amazonaws.com/bcl/all-in-one` |
| Load balancer | `bcl-alb` |
| Bedrock model | `amazon.nova-lite-v1:0` (Converse API) |
| CloudWatch logs | `/bcl/all-in-one` |

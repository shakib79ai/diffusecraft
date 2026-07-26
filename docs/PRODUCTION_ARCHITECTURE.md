# Production Architecture (AWS)

> **Scope note.** The root of this repo (`app.py` + `DiffuseCraft_Colab.ipynb`) is the
> open-source demo: a single-process Gradio app meant for a local GPU or a free Colab
> runtime. This document specifies the architecture required to run DiffuseCraft as a
> **multi-tenant, publicly hosted production service** on AWS — the target design for
> a `server/` API service that wraps the same model-loading core shown in `app.py`.
> Treat this as the engineering spec / RFC for that build-out, not as already-deployed
> infrastructure.
>
> Companion documents: [`API_REFERENCE.md`](API_REFERENCE.md) (REST contract),
> [`COST_ESTIMATE.md`](COST_ESTIMATE.md) (monthly AWS cost by tier),
> [`SECURITY.md`](SECURITY.md) (controls, IAM, compliance).

## Table of Contents

1. [Design Goals & Constraints](#design-goals--constraints)
2. [High-Level Architecture](#high-level-architecture)
3. [Networking (VPC)](#networking-vpc)
4. [Compute — GPU Inference Tier](#compute--gpu-inference-tier)
5. [Asynchronous Job Pipeline](#asynchronous-job-pipeline)
6. [Database — PostgreSQL (RDS)](#database--postgresql-rds)
7. [Vector Database](#vector-database)
8. [Caching Layer](#caching-layer)
9. [Object Storage (S3)](#object-storage-s3)
10. [Load Balancing & Traffic Management](#load-balancing--traffic-management)
11. [Domain, DNS & CDN](#domain-dns--cdn)
12. [API Layer](#api-layer)
13. [Security](#security)
14. [Observability](#observability)
15. [CI/CD & Infrastructure as Code](#cicd--infrastructure-as-code)
16. [Scaling Strategy](#scaling-strategy)
17. [Disaster Recovery & Backups](#disaster-recovery--backups)
18. [Phased Rollout Plan](#phased-rollout-plan)

---

## Design Goals & Constraints

| Constraint | Implication |
|---|---|
| Inference requires a full GPU (SD 1.5/2.1 needs ~6-10GB VRAM resident) | No Fargate (no GPU support) — must run on EC2, ECS-on-EC2, EKS-on-EC2, or SageMaker |
| Single generation takes 2-15s depending on steps/resolution | Request/response over a plain synchronous HTTP call is viable for p50, but must design for an **async job pattern** to avoid ALB/API Gateway 30-60s timeout cliffs under load |
| GPU instances are the dominant cost (~70-85% of total spend, see [COST_ESTIMATE.md](COST_ESTIMATE.md)) | Autoscale aggressively, prefer Spot/scale-to-zero, avoid over-provisioning |
| Open pretrained checkpoints have no reliable built-in safety filter (`safety_checker=None` in `app.py`) | Production must add an explicit output moderation step — see [SECURITY.md](SECURITY.md#content-moderation) |
| Public, multi-tenant service | Needs auth, per-key rate limiting, metering for billing, and standard perimeter security (WAF, TLS, DDoS protection) |

## High-Level Architecture

```
                                   ┌─────────────────────┐
                                   │   Route 53 (DNS)     │
                                   │ diffusecraft.ai zone  │
                                   └──────────┬───────────┘
                                              │
                     ┌────────────────────────┼─────────────────────────┐
                     │                                                  │
             app.diffusecraft.ai                                 api.diffusecraft.ai
                     │                                                  │
           ┌─────────▼──────────┐                             ┌─────────▼──────────┐
           │   CloudFront (CDN)  │                             │  CloudFront (edge)  │
           │  + AWS WAF Web ACL  │                             │  + AWS WAF Web ACL  │
           └─────────┬──────────┘                             └─────────┬──────────┘
                     │                                                  │
           ┌─────────▼──────────┐                             ┌─────────▼──────────┐
           │ S3 (static frontend│                             │  Application Load   │
           │  / docs / landing) │                             │  Balancer (public)  │
           └─────────────────────┘                             └─────────┬──────────┘
                                                                          │
                                          ┌───────────────────────────────┼───────────────────────────────┐
                                          │                    VPC (private subnets, 2+ AZs)               │
                                          │                                │                               │
                                ┌─────────▼──────────┐          ┌──────────▼─────────┐          ┌──────────▼─────────┐
                                │  ECS Service: API   │          │  ECS Service:       │          │   Amazon SQS        │
                                │  (FastAPI, CPU-only,│─────────▶│  GPU Worker         │◀────────▶│  generation-queue    │
                                │  Fargate, autoscaled│  enqueue │  (EC2 g5.xlarge,    │  poll    │  + dead-letter queue │
                                │  on request count)  │          │  ECS-on-EC2, GPU AMI)│          └──────────────────────┘
                                └─────────┬───────────┘          └──────────┬─────────┘
                                          │                                │
                     ┌────────────────────┼────────────────────────────────┼────────────────────┐
                     │                    │                                │                    │
           ┌─────────▼──────────┐ ┌───────▼────────┐              ┌────────▼─────────┐ ┌─────────▼──────────┐
           │  RDS PostgreSQL     │ │ ElastiCache     │              │  S3: generated    │ │  RDS PostgreSQL     │
           │  (jobs, users,      │ │ Redis (rate     │              │  images bucket     │ │  + pgvector ext.     │
           │  api_keys, billing) │ │ limit, cache)   │              │  (versioned, CDN'd)│ │  (prompt embeddings) │
           └──────────────────────┘ └─────────────────┘              └─────────────────────┘ └──────────────────────┘

  Cross-cutting: IAM roles · Secrets Manager · KMS · CloudWatch + X-Ray · GuardDuty · CloudTrail · AWS Config
```

**Request flow (async job pattern):**

1. Client calls `POST /v1/generate` on `api.diffusecraft.ai` (through CloudFront → WAF → ALB).
2. The **API service** (stateless FastAPI containers on Fargate) authenticates the API key, validates the payload, checks the Redis rate limiter, writes a `jobs` row (`status=queued`) to RDS, and pushes a message to SQS. Returns `202 Accepted` with `job_id` in <100ms.
3. A **GPU worker** (ECS task pinned to a `g5.xlarge` EC2 instance) long-polls SQS, pulls the job, loads/reuses the cached pipeline, runs inference, uploads the PNG to S3, updates the `jobs` row to `status=succeeded` with the S3 key, and optionally publishes to an SNS topic for webhook delivery.
4. Client polls `GET /v1/jobs/{job_id}` (or receives a webhook) and then fetches the image via the CloudFront-fronted S3 URL.

This decouples the fast, cheap control plane (API/auth/billing) from the slow, expensive data plane (GPU inference), which is the standard pattern for GPU-backed SaaS APIs (also how Replicate/Stability's own hosted APIs are structured).

## Networking (VPC)

| Component | Detail |
|---|---|
| VPC CIDR | `10.20.0.0/16` |
| Public subnets | 2 (one per AZ) — hold the ALB and NAT Gateways only |
| Private subnets | 2 (one per AZ) — hold ECS tasks (API + GPU workers), RDS, ElastiCache |
| NAT Gateway | One per AZ (HA) — private subnet egress for pulling model weights from the Hugging Face Hub, OS patches |
| VPC Endpoints | Gateway endpoint for S3 (avoids NAT data-processing charges for model/image traffic), Interface endpoints for SQS, Secrets Manager, ECR, CloudWatch Logs (keeps AWS-service traffic off the public internet and cuts NAT cost) |
| Security Groups | `sg-alb` (443 from `0.0.0.0/0`) → `sg-api` (8000 from `sg-alb` only) → `sg-rds` (5432 from `sg-api`/`sg-worker` only), `sg-worker` (no inbound; outbound to SQS/S3/HF Hub/RDS/Redis only), `sg-redis` (6379 from `sg-api`/`sg-worker` only) |

## Compute — GPU Inference Tier

**Recommended: Amazon ECS (EC2 launch type) with a GPU-optimized capacity provider.**

| Choice | Why |
|---|---|
| Instance type | `g5.xlarge` (1× NVIDIA A10G, 24GB VRAM, 4 vCPU, 16GB RAM) — comfortably runs SD 1.5/2.1 at 512-768px in fp16 with headroom for batching |
| AMI | Amazon ECS GPU-optimized AMI (Bottlerocket or AL2-based; ships NVIDIA driver + `nvidia-container-toolkit` preconfigured) |
| Orchestration | ECS Auto Scaling Group **Capacity Provider**, target-tracking on `ECSServiceAverageCPUUtilization` **and** a custom CloudWatch metric for SQS `ApproximateNumberOfMessagesVisible` per instance |
| Task sizing | 1 GPU worker task per EC2 instance (SD needs the whole GPU's VRAM; no bin-packing multiple tasks per GPU) |
| Purchasing | Mixed instances policy: 1 On-Demand baseline instance (always warm, avoids cold-start latency) + Spot for burst capacity (Spot typically 60-70% cheaper; workers are stateless and idempotent via SQS visibility timeout + retry, so Spot interruption just re-queues the job) |
| Scale to zero (dev/staging only) | For non-prod environments, scale the ASG's desired count to 0 outside business hours via a scheduled Lambda/EventBridge rule, or use **SageMaker Asynchronous Inference** instead of ECS entirely — it natively scales to zero and bills per-second, which is usually cheaper than an always-on staging GPU box |

**Alternative considered — SageMaker Asynchronous Inference.** Purpose-built for exactly this workload (queued, long-running GPU inference with S3 in/out and native scale-to-zero). Recommended over ECS once request volume is low/spiky (staging, early production) since you stop paying for idle GPU minutes entirely. Revisit as the primary compute layer if traffic patterns turn out to be bursty rather than steady — see the trade-off table in [COST_ESTIMATE.md](COST_ESTIMATE.md#compute-option-tradeoffs).

## Asynchronous Job Pipeline

- **Queue:** Amazon SQS standard queue `diffusecraft-generation-queue`, visibility timeout 120s (> p99 generation time), `maxReceiveCount=3` before moving to a dead-letter queue `diffusecraft-generation-dlq`.
- **Idempotency:** each job carries a client-supplied or server-generated `idempotency_key`; the API upserts on that key so retried `POST /v1/generate` calls don't double-enqueue.
- **Failure handling:** DLQ messages trigger a CloudWatch alarm → SNS → on-call notification; failed jobs are marked `status=failed` in RDS with an `error_code` the client can read back from `GET /v1/jobs/{id}`.
- **Backpressure:** if `ApproximateNumberOfMessagesVisible` exceeds a threshold and the ASG is already at `MaxSize`, the API starts returning `429 Too Many Requests` with a `Retry-After` header rather than enqueueing unboundedly.

## Database — PostgreSQL (RDS)

- **Engine:** Amazon RDS for PostgreSQL 16.
- **Sizing:** `db.t4g.medium` (staging) → `db.r6g.large` (production), Multi-AZ enabled in production for automatic failover.
- **Connection management:** Amazon RDS Proxy in front of RDS — ECS tasks scale independently of DB connection limits, and it holds connections through Spot worker interruptions/restarts.
- **Core schema (abridged):**

```sql
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE api_keys (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    key_hash TEXT NOT NULL,              -- SHA-256 of the secret; raw key shown once at creation
    rate_limit_per_min INT NOT NULL DEFAULT 30,
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    idempotency_key TEXT,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued | processing | succeeded | failed
    prompt TEXT NOT NULL,
    negative_prompt TEXT,
    model_id TEXT NOT NULL,
    params JSONB NOT NULL,                   -- steps, cfg, width, height, seed, scheduler
    error_code TEXT,
    s3_key TEXT,
    prompt_embedding VECTOR(384),            -- pgvector column, see below
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE usage_events (                  -- metering for billing
    id BIGSERIAL PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    gpu_seconds NUMERIC NOT NULL,
    billed_at TIMESTAMPTZ
);
```

- **Backups:** automated daily snapshots, 7-day (staging) / 30-day (production) retention, plus point-in-time recovery enabled.

## Vector Database

**Recommended: `pgvector` extension on the same RDS PostgreSQL instance** (the `prompt_embedding` column above), rather than standing up a separate vector database service. Rationale:

| Use case | How it's used |
|---|---|
| Semantic response caching | Embed the incoming prompt (small sentence-transformer, e.g. `all-MiniLM-L6-v2`, run on the CPU-only API tier); an `ORDER BY prompt_embedding <=> $1 LIMIT 1` cosine search finds near-duplicate prior prompts within a similarity threshold and can serve a cached image, skipping GPU inference entirely |
| Content moderation | Compare prompt embeddings against a curated blocklist embedding set to flag likely policy-violating requests before they reach the GPU |
| "Similar prompts" / analytics | Power a "people also generated" feature without a separate system |

`pgvector` with an `ivfflat` or `hnsw` index comfortably handles millions of rows at this scale, and keeping it in Postgres avoids a second database to operate, back up, and secure.

**When to graduate to a dedicated vector database:** if embedding volume grows past tens of millions of rows, or query latency/throughput on the shared RDS instance becomes a bottleneck, migrate to **Amazon OpenSearch Service** (k-NN plugin — stays inside the AWS account/IAM boundary) or a managed SaaS option like **Pinecone** (less ops, usage-based pricing, easiest migration path since it's a drop-in swap behind a small `VectorStore` interface). Neither is needed at MVP scale.

## Caching Layer

- **Amazon ElastiCache for Redis** (`cache.t4g.micro` → `cache.r6g.large` as traffic grows), Multi-AZ with automatic failover in production.
- Responsibilities:
  - **Rate limiting** — sliding-window counters per API key (`INCR` + `EXPIRE`), enforced in API middleware before a request ever reaches RDS/SQS.
  - **Job status cache** — hot read-through cache for `GET /v1/jobs/{id}` polling, so repeated polling doesn't hammer RDS.
  - **Exact-match prompt cache** — a cheap Redis lookup (hash of normalized prompt+params) before falling back to the pgvector semantic search.

## Object Storage (S3)

| Bucket | Purpose | Policy |
|---|---|---|
| `diffusecraft-model-cache` | Cached Hugging Face model weights, pre-warmed at AMI-bake time / on worker boot | Private, versioned off, lifecycle: none (long-lived) |
| `diffusecraft-generated-images` | Output PNGs | Private (served via CloudFront OAC, never public), SSE-KMS encryption, lifecycle: transition to S3 Infrequent Access after 30 days, delete after 90 days (configurable per plan tier) |
| `diffusecraft-access-logs` | ALB/CloudFront/S3 access logs | Private, Object Lock (compliance mode) for tamper-evident audit trail, lifecycle: expire after 1 year |

CloudFront sits in front of `diffusecraft-generated-images` using an **Origin Access Control (OAC)** so the bucket itself stays fully private — this is both a cost win (CDN caching cuts S3 GET/data-transfer costs) and a security requirement (see [SECURITY.md](SECURITY.md)).

## Load Balancing & Traffic Management

- **Application Load Balancer** (internet-facing, public subnets), HTTPS listener on 443 (ACM cert), HTTP:80 redirects to HTTPS.
- **Target group:** the API service's ECS tasks (Fargate, `awsvpc` networking), health check `GET /health` (expects `200` only when DB + Redis + SQS connectivity all check out), deregistration delay 30s for graceful in-flight request draining.
- **Autoscaling (API tier):** ECS Service Auto Scaling on `ALBRequestCountPerTarget` target-tracking policy — this tier is cheap (CPU-only Fargate) so it scales aggressively and independently of the GPU tier.
- **Autoscaling (GPU tier):** see [Compute](#compute--gpu-inference-tier) — scales on queue depth, not request count, since it's decoupled behind SQS.
- **Cross-zone load balancing:** enabled (default for ALB) to spread load evenly across AZ-imbalanced task counts.
- GPU workers are **not** behind the ALB at all — they only pull from SQS, which is itself a highly available, managed load-balancing mechanism for the async tier.

## Domain, DNS & CDN

- **Registrar / DNS:** domain registered and hosted-zone managed in **Route 53** (or an external registrar with NS delegation to a Route 53 hosted zone — functionally identical).
- **Records:**
  - `app.diffusecraft.ai` → A/AAAA alias → CloudFront distribution (frontend/docs)
  - `api.diffusecraft.ai` → A/AAAA alias → CloudFront distribution → ALB origin (see below), *or* a direct alias straight to the ALB if the API doesn't need edge caching
  - `images.diffusecraft.ai` (optional) → CloudFront distribution → S3 `diffusecraft-generated-images` origin
- **TLS:** AWS Certificate Manager, DNS-validated. CloudFront certs must be issued in `us-east-1`; ALB certs are issued in the app's deployment region.
- **CDN:** CloudFront in front of both the API (to terminate TLS at the edge, absorb DDoS at the AWS edge network, and attach WAF) and the images bucket (for actual caching/latency benefit — generated images are immutable once created, so they're highly cacheable with long `Cache-Control` TTLs and content-addressed keys).

## API Layer

Full request/response contract lives in [API_REFERENCE.md](API_REFERENCE.md). Summary:

- **Framework:** FastAPI (async), run under `uvicorn`/`gunicorn` workers on Fargate.
- **Auth:** `Authorization: Bearer <api_key>` — key hashed and looked up against `api_keys.key_hash`; optional upgrade path to Amazon Cognito if/when a hosted user dashboard with email/password or social login is added (not required for a pure API product).
- **Versioning:** URI-versioned (`/v1/...`); breaking changes ship as `/v2` rather than mutating `/v1` in place.
- **Core endpoints:** `POST /v1/generate`, `GET /v1/jobs/{job_id}`, `GET /v1/models`, `GET /health`.
- **Rate limiting:** per-key sliding window in Redis (default 30 req/min, configurable per key), `429` + `Retry-After` on breach; a second, coarser rate-based rule lives in WAF as a perimeter backstop against abuse that bypasses the app layer entirely.

## Security

Full detail in [SECURITY.md](SECURITY.md). Highlights: private subnets for all compute/data, least-privilege IAM roles per ECS task (no shared credentials, no long-lived AWS keys in containers), Secrets Manager for DB/Redis/HF-token credentials with automatic rotation, KMS encryption at rest everywhere (RDS, S3, EBS, ElastiCache), AWS WAF managed rule groups + rate-based rules on both CloudFront distributions, GuardDuty + Security Hub for threat detection, CloudTrail (multi-region, S3 Object Lock) for audit logging, and a mandatory output content-moderation step (Amazon Rekognition moderation labels) before any generated image is returned to a client.

## Observability

- **Logs:** structured JSON logs from both API and worker containers → CloudWatch Logs (via the `awslogs` ECS log driver), with a subscription filter streaming errors to an alerting Lambda.
- **Metrics:** CloudWatch (ECS service CPU/memory, ALB request/latency/5xx, SQS queue depth/age-of-oldest-message, RDS/ElastiCache standard metrics), plus a custom GPU-utilization metric published from workers via the CloudWatch agent + `nvidia-smi`.
- **Tracing:** AWS X-Ray across API → SQS → worker → RDS/S3 for end-to-end latency breakdown per job.
- **Dashboards & alarms:** CloudWatch Dashboard per environment; alarms → SNS → Slack/PagerDuty for: 5xx rate, p99 latency, DLQ depth > 0, GPU ASG at `MaxSize` for >10min, RDS storage/CPU thresholds.
- **Cost visibility:** AWS Budgets + Cost Anomaly Detection, tagged by environment/service for cost allocation.

## CI/CD & Infrastructure as Code

- **IaC:** Terraform, remote state in a versioned/encrypted S3 bucket with a DynamoDB lock table; separate workspaces for `dev` / `staging` / `production`.
- **Pipeline (GitHub Actions):**
  1. Lint + unit tests on every PR.
  2. On merge to `main`: build Docker image (see repo-root `Dockerfile`), push to **Amazon ECR**, tag with commit SHA.
  3. `terraform plan` posted as a PR comment for infra changes; `terraform apply` on merge, gated by manual approval for production.
  4. ECS rolling deployment (`aws ecs update-service --force-new-deployment`) for the API tier; the GPU worker tier deploys via a similar rolling update with `minimumHealthyPercent`/`maximumPercent` tuned so at least one GPU worker stays warm during rollout.
- **Environment promotion:** identical Terraform modules across `dev → staging → production`, differing only by tfvars (instance sizes, Multi-AZ on/off, autoscaling bounds).

## Scaling Strategy

| Layer | Scales on | Min → Max (production) |
|---|---|---|
| API (Fargate) | ALB request count per target | 2 → 20 tasks |
| GPU workers (ECS-on-EC2) | SQS queue depth (custom metric) | 1 On-Demand + 0 Spot → 1 On-Demand + 9 Spot |
| RDS | Vertical (instance class) + read replica if read-heavy (e.g. dashboard/analytics queries) | Single writer → writer + 1-2 read replicas |
| ElastiCache | Vertical + cluster mode if a single shard becomes a bottleneck | 1 node → cluster mode, 3 shards |

## Disaster Recovery & Backups

- **RTO/RPO targets:** RPO ≤ 5 minutes (RDS automated backups + PITR), RTO ≤ 30 minutes (Multi-AZ automatic failover for RDS/ElastiCache; ECS/ASG simply reschedules tasks in a healthy AZ).
- **Region strategy:** single-region (e.g. `us-east-1`) for MVP/production v1 — GPU capacity and Spot availability are more constrained across regions, so multi-region active/active is deferred until traffic genuinely requires it. Cross-region S3 replication for the generated-images bucket is a cheap first step toward regional resilience if needed sooner.
- **Runbooks:** documented failover steps for RDS Multi-AZ failover, ASG instance replacement, and SQS DLQ redrive, exercised periodically via game days.

## Phased Rollout Plan

| Phase | What | Infra |
|---|---|---|
| **0 — OSS demo** (current state of this repo) | Gradio app, Colab notebook, `share=True` public link | None — local GPU or free Colab |
| **1 — MVP** | FastAPI wrapper + single GPU worker, no autoscaling, single-AZ RDS | 1× EC2 `g5.xlarge`, `db.t4g.micro`, no CloudFront/WAF yet, IP-restricted or API-key-only access |
| **2 — Production** | Full architecture above: async SQS pipeline, autoscaling both tiers, Multi-AZ RDS/Redis, WAF/CloudFront, CI/CD | As specified in this document |
| **3 — Scale** | SageMaker Async Inference or multi-region evaluated based on real traffic shape; dedicated vector DB if pgvector becomes a bottleneck; read replicas; Reserved Instance/Savings Plan commitments for baseline GPU capacity | Incremental on top of Phase 2 |

See [COST_ESTIMATE.md](COST_ESTIMATE.md) for the monthly cost of each phase.

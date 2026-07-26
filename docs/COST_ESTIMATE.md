# AWS Cost Estimate

> Figures are **illustrative estimates** using approximate `us-east-1` on-demand
> pricing at the time of writing, for the architecture in
> [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md). AWS pricing changes over
> time and varies by region — validate against the
> [AWS Pricing Calculator](https://calculator.aws) before budgeting. All numbers are
> monthly USD unless noted.

## TL;DR

GPU compute is **70-85% of total spend** at every tier. Every other cost optimization
(Reserved Instances aside) is secondary to how you buy and schedule GPU capacity —
see [Cost Optimization Levers](#cost-optimization-levers) below.

## Tier 1 — MVP / Staging (Phase 1)

Single always-on GPU worker, single-AZ database, minimal redundancy. Suitable for a
soft launch or internal staging environment.

| Component | Configuration | Est. monthly cost |
|---|---|---|
| GPU compute | 1× `g5.xlarge` On-Demand, 24/7 | ~$730 |
| API compute | Fargate, 1 task × 0.5 vCPU/1GB, 24/7 | ~$15 |
| Load balancer | 1 ALB, low traffic | ~$20 |
| Database | RDS `db.t4g.micro`, single-AZ | ~$15 |
| Cache | ElastiCache `cache.t4g.micro`, single node | ~$12 |
| Object storage | S3, <50GB + minimal requests | ~$5 |
| CDN | CloudFront, low traffic | ~$5 |
| DNS | Route 53 hosted zone + queries | ~$1 |
| NAT Gateway | 1 (single AZ) + data processing | ~$35 |
| Secrets/KMS | Secrets Manager (~5 secrets) + KMS keys | ~$5 |
| Monitoring | CloudWatch logs/metrics/alarms | ~$10 |
| Security baseline | GuardDuty + CloudTrail (light usage) | ~$15 |
| **Total** | | **≈ $865/month** |

**Cheaper alternative for this tier:** replace the always-on `g5.xlarge` with
**SageMaker Asynchronous Inference** (scales to zero between requests, billed
per-second of actual inference). For a staging environment handling, say, 500
generations/day at ~6s each, that's roughly 50 GPU-minutes/day → **~$60-100/month** in
compute instead of $730 — a large enough gap that Phase 1 should default to SageMaker
Async unless you specifically need the ECS architecture validated pre-launch.

## Tier 2 — Production (Phase 2)

Autoscaled GPU tier (baseline + burst), Multi-AZ database/cache, full security/observability
stack, moderate sustained traffic (illustrative: ~20,000 generations/day average).

| Component | Configuration | Est. monthly cost |
|---|---|---|
| GPU compute (baseline) | 2× `g5.xlarge` On-Demand, 24/7 | ~$1,460 |
| GPU compute (burst) | Spot autoscaling, avg 2 extra instances during peak hours (~60% Spot discount) | ~$450 |
| API compute | Fargate, autoscaled 2-8 tasks avg 4 | ~$60 |
| Load balancer | 1 ALB + LCU usage at this traffic | ~$60 |
| Database | RDS `db.r6g.large`, Multi-AZ | ~$420 |
| Cache | ElastiCache `cache.r6g.large`, Multi-AZ | ~$180 |
| Object storage | S3, ~1TB images + requests | ~$60 |
| CDN | CloudFront, moderate traffic | ~$80 |
| DNS | Route 53 hosted zone + queries | ~$3 |
| NAT Gateway | 2 (Multi-AZ) + data processing | ~$90 |
| Secrets/KMS | Secrets Manager + KMS | ~$8 |
| Monitoring | CloudWatch + X-Ray, production volume | ~$60 |
| Security | GuardDuty + Security Hub + CloudTrail + Config | ~$100 |
| WAF | Web ACL + managed rule groups + requests | ~$30 |
| **Total** | | **≈ $3,060/month** |

This is a baseline estimate for *moderate* sustained traffic; it scales roughly
linearly with GPU-hours consumed once the burst tier is the dominant driver.

## Compute Option Tradeoffs

| Option | Best for | Cost model | Cold start |
|---|---|---|---|
| **ECS-on-EC2 (g5.xlarge), always-on baseline + Spot burst** | Steady, predictable traffic; lowest per-request latency | Pay for provisioned capacity whether used or not (baseline), Spot for burst | None (baseline always warm) |
| **SageMaker Asynchronous Inference** | Spiky/low/unpredictable traffic; staging | Pay per-second of actual GPU inference time; scales to zero | Cold start when scaling from zero (tens of seconds to load model) |
| **EC2 Spot-only ASG (no On-Demand baseline)** | Cost-sensitive workloads that can tolerate occasional interruption/retry | Cheapest steady-state compute (~60-70% off) | Possible full cold start if Spot capacity is reclaimed and pool is thin |

Recommendation: start with SageMaker Async for Phase 1, move the baseline to
On-Demand ECS-on-EC2 once traffic is steady enough that scale-to-zero no longer
saves money net of cold-start latency, and always burst with Spot.

## Cost Optimization Levers

1. **GPU purchasing strategy** (highest-leverage lever by far):
   - EC2 **Savings Plans** (1 or 3-year commitment) on the baseline GPU instances: 30-50% off On-Demand.
   - **Spot Instances** for burst capacity: 60-70% off, acceptable here because jobs are idempotent and requeue automatically via SQS visibility timeout.
   - **Scale-to-zero** (SageMaker Async, or scheduled ASG desired-count=0 outside business hours) for any non-production environment.
2. **Batching:** batch multiple queued prompts into a single forward pass where params match (steps/resolution) — reduces GPU-seconds per image at moderate-to-high queue depth.
3. **Model/resolution defaults:** default to 512×512 / 25 steps; higher resolutions and step counts scale compute cost roughly linearly (steps) to quadratically (resolution).
4. **CloudFront caching:** since generated images are immutable, long `Cache-Control` TTLs on the images distribution cut S3 GET and data-transfer costs substantially for any image viewed more than once (shares, retries, previews).
5. **VPC Gateway Endpoint for S3:** avoids NAT Gateway data-processing charges ($0.045/GB) on model-weight downloads and image uploads, which is meaningful at volume.
6. **Right-size non-GPU tiers regularly:** RDS/ElastiCache instance classes and Fargate task sizing should be revisited against actual CloudWatch utilization every quarter — these are small relative to GPU cost but easy to over-provision by default.

## What's *not* included above

- Data transfer OUT to the internet beyond typical usage patterns (large spikes in image downloads would add to the CloudFront/S3 line items).
- Third-party costs (domain registration ~$12-15/year if not using Route 53 Domains, any paid monitoring/alerting integrations like PagerDuty).
- Engineering/operational time.

# Security Architecture

> Security controls for the production deployment described in
> [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md). Organized by layer,
> outside-in.

## 1. Perimeter & Network

| Control | Detail |
|---|---|
| DDoS protection | AWS Shield Standard (automatic, free, applies to CloudFront/ALB/Route 53); evaluate Shield Advanced if the service becomes a high-value target or needs SLA-backed DDoS response |
| Web Application Firewall | AWS WAF Web ACL attached to **both** CloudFront distributions (frontend and API edge) and the ALB, with: AWS Managed Core Rule Set, Known Bad Inputs rule group, SQLi rule group, and a rate-based rule (e.g. 2,000 requests / 5 min / IP) as a backstop above the app-level per-key limiter |
| Network isolation | VPC with public subnets holding only the ALB + NAT Gateways; all compute (ECS tasks) and data stores (RDS, ElastiCache) in private subnets with no route to the internet except via NAT |
| Security Groups | Least-privilege, chained: `sg-alb` (443 from `0.0.0.0/0`) → `sg-api` (app port from `sg-alb` only) → `sg-rds`/`sg-redis` (DB/cache ports from `sg-api`/`sg-worker` only). No security group allows broad `0.0.0.0/0` ingress except the ALB's 443. |
| TLS everywhere | ACM-issued certs on CloudFront and ALB; HTTP→HTTPS redirect enforced; TLS 1.2+ minimum policy on both CloudFront and ALB listeners |
| VPC Endpoints | Interface/Gateway endpoints for S3, SQS, Secrets Manager, ECR, CloudWatch Logs — keeps AWS-service traffic off the public internet entirely |

## 2. Identity & Access Management

| Control | Detail |
|---|---|
| Task-level IAM roles | Each ECS task (API, worker) gets its own IAM role scoped to exactly what it needs — e.g. the worker role can `s3:PutObject` only on the generated-images bucket prefix and `sqs:ReceiveMessage`/`DeleteMessage` only on its queue; it cannot read other services' resources |
| No long-lived credentials in containers | All AWS access via IAM roles (ECS task roles / instance profiles); no static AWS access keys baked into images, env vars, or repo |
| Human access | IAM users/roles for engineers require MFA; production console/API access goes through an SSO-federated role (IAM Identity Center) with time-bounded sessions, not standing IAM users |
| Permission boundaries | IAM permission boundaries applied to CI/CD deployment roles so a compromised pipeline can't escalate beyond the resources Terraform is meant to touch |
| Database access | Application connects via RDS Proxy using IAM database authentication where feasible, avoiding static DB passwords in app config |

## 3. Secrets Management

- All credentials (DB password, Redis auth token, Hugging Face access token, webhook
  signing secret) live in **AWS Secrets Manager**, injected into ECS tasks at runtime
  via the `secrets` block in the task definition (never baked into the image or
  committed to the repo).
- **Automatic rotation** enabled for the RDS master credential (Secrets Manager's
  built-in RDS rotation Lambda).
- API keys shown to end users are never stored in plaintext — only a salted SHA-256
  hash lives in `api_keys.key_hash`; the raw key is shown once at creation and cannot
  be retrieved again (standard practice, same as GitHub PATs/Stripe keys).

## 4. Encryption

| Data | At rest | In transit |
|---|---|---|
| RDS | SSE via KMS customer-managed key (CMK) | TLS enforced (`rds.force_ssl=1`) |
| ElastiCache | Encryption-at-rest + in-transit enabled | TLS |
| S3 (all buckets) | SSE-KMS, bucket policies deny unencrypted `PutObject` | HTTPS-only bucket policy (`aws:SecureTransport` condition) |
| EBS (ECS EC2 hosts) | KMS-encrypted volumes by default (enforced via AWS Config rule) | n/a |
| Secrets Manager | KMS-encrypted by default | TLS (AWS SDK) |

CMKs are scoped per data class (separate keys for RDS, S3-images, Secrets Manager) so
key rotation/revocation for one doesn't require touching the others.

## 5. Application-Layer Security

- **Input validation:** Pydantic models on every FastAPI endpoint reject malformed
  payloads before they reach business logic (see [API_REFERENCE.md](API_REFERENCE.md)).
- **Authentication:** bearer API keys, hashed and rate-limited per key (see API
  reference); optional Cognito upgrade path if a hosted dashboard with user
  login is added later.
- **Authorization:** every DB query scoped by `user_id` derived from the authenticated
  key — no client-suppliable ID is ever trusted for row access.
- **Prompt-injection / abuse surface:** since prompts are only ever used as diffusion
  model input (not interpolated into SQL, shell commands, or another LLM's system
  prompt), classic injection risk is low; the main abuse vector is *content* policy
  violation, handled by moderation below, and volumetric abuse, handled by rate
  limiting/WAF.
- **Dependency hygiene:** `pip-audit`/`safety` (or GitHub Dependabot) run in CI against
  `requirements.txt`; container base images scanned by **Amazon ECR image scanning**
  (or Trivy in CI) on every push.

## 6. Content Moderation

The OSS demo (`app.py`) explicitly disables the diffusers safety checker
(`safety_checker=None`) for transparency and speed on a single-user demo. **This must
not ship as-is in the multi-tenant production service.** Production adds:

1. **Pre-generation prompt filtering:** the pgvector-based similarity check against a
   blocklist embedding set (see [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md#vector-database))
   plus a keyword/heuristic filter, rejecting requests with `403 content_policy_violation`
   before they reach the GPU.
2. **Post-generation output moderation:** every generated image is checked with
   **Amazon Rekognition's `DetectModerationLabels`** before the job is marked
   `succeeded` and the URL is returned to the client. Flagged images are quarantined
   (not served), the job is marked `failed` with `error_code=moderation_flagged`, and
   repeated violations by the same API key feed into an abuse-scoring system that can
   auto-suspend the key.
3. **Audit trail:** moderation decisions (both pre- and post-generation) are logged
   with the job record for appeals/review and for demonstrating due diligence.

## 7. Threat Detection & Audit

| Service | Purpose |
|---|---|
| **Amazon GuardDuty** | Continuous threat detection across the account (anomalous API calls, compromised credentials, crypto-mining patterns on EC2 — relevant given GPU instances are a common cryptomining target if compromised) |
| **AWS Security Hub** | Aggregates GuardDuty + Config + Inspector findings into a single compliance-posture view (CIS AWS Foundations Benchmark) |
| **AWS CloudTrail** | Multi-region trail, log file validation enabled, delivered to an S3 bucket with **Object Lock** (compliance mode) so audit logs are tamper-evident even from account admins |
| **AWS Config** | Continuous compliance rules — e.g. "no unencrypted EBS volumes," "no security group with unrestricted SSH," "S3 buckets must not be public" — auto-remediated where safe to do so |
| **Amazon Inspector** | Automated vulnerability scanning of ECS container images and EC2 instances |

## 8. Data Protection & Privacy

- **Data minimization:** prompts and generated images are retained only as long as
  needed for the product experience (default 90-day lifecycle on the images bucket,
  configurable per plan — see [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md#object-storage-s3)).
- **Right to deletion:** a `DELETE /v1/users/{id}` admin/self-service path removes the
  user's `jobs`/`usage_events` rows and issues S3 deletes for their generated images,
  supporting GDPR/CCPA-style deletion requests.
- **PII surface is intentionally small:** the only PII collected is an email address
  for account/billing purposes; no payment card data is stored directly (a PCI-scope
  payment processor like Stripe should be used for billing, keeping card data entirely
  out of this system).

## 9. Incident Response (baseline)

1. CloudWatch/GuardDuty alarms page on-call via SNS → PagerDuty/Slack.
2. Runbook: isolate (revoke IAM credentials / API key, adjust security groups) →
   assess (CloudTrail + application logs) → remediate → post-incident review.
3. Secrets rotation triggered immediately for any credential suspected of exposure,
   independent of the automatic rotation schedule.

## 10. Pre-Launch Security Checklist

- [ ] All S3 buckets confirmed private, `BlockPublicAccess` enabled account-wide
- [ ] WAF Web ACLs attached and in **blocking** (not count-only) mode before public launch
- [ ] GuardDuty + Security Hub + Config enabled in the account
- [ ] CloudTrail multi-region trail with Object Lock verified delivering logs
- [ ] Secrets Manager rotation confirmed working (test rotation run)
- [ ] Output content moderation (Rekognition) verified against known-bad test images
- [ ] Load test confirms rate limiting/WAF rules trigger correctly under abuse simulation
- [ ] IAM Access Analyzer run with zero unintended external-access findings
- [ ] Penetration test / third-party security review completed for the API surface

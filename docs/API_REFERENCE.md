# API Reference (Production Service)

> Target contract for the `server/` FastAPI service described in
> [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md). This is the proposed API
> surface for hosting DiffuseCraft as a multi-tenant service — the OSS demo (`app.py`)
> exposes a Gradio UI, not this REST API.

Base URL: `https://api.diffusecraft.ai/v1`

## Authentication

All requests (except `/health`) require an API key:

```
Authorization: Bearer dc_live_51H8x...
```

Keys are issued per user, stored server-side as a salted SHA-256 hash (`api_keys.key_hash`
in RDS — see the schema in [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md#database--postgresql-rds)),
and shown to the user exactly once at creation time. Missing/invalid keys return `401`.

## Rate Limits

- Default: **30 requests/minute** per API key (configurable per key/plan).
- Enforced via a Redis sliding-window counter in the API middleware; a coarser
  IP-based rate-based rule also runs at the WAF layer as a perimeter backstop.
- Responses include standard rate-limit headers:

```
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 27
X-RateLimit-Reset: 1706284800
```

Breaching the limit returns `429 Too Many Requests` with a `Retry-After` header.

## Endpoints

### `POST /v1/generate`

Enqueue a text-to-image generation job. Returns immediately (async job pattern —
see [PRODUCTION_ARCHITECTURE.md](PRODUCTION_ARCHITECTURE.md#asynchronous-job-pipeline)).

**Request body**

```json
{
  "prompt": "A cinematic photo of a red fox in a snowy forest, golden hour lighting",
  "negative_prompt": "blurry, low quality, watermark",
  "model": "sd-1.5",
  "scheduler": "dpm-2m",
  "steps": 25,
  "guidance_scale": 7.5,
  "width": 512,
  "height": 512,
  "seed": -1,
  "idempotency_key": "client-generated-uuid-optional"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `prompt` | string | yes | 1-1000 chars |
| `negative_prompt` | string | no | default applied server-side if omitted |
| `model` | enum | no | `sd-1.5` (default) \| `sd-2.1` \| `dreamlike-photoreal` \| `openjourney` |
| `scheduler` | enum | no | `dpm-2m` (default) \| `euler-a` |
| `steps` | int | no | 10-50, default 25 |
| `guidance_scale` | float | no | 1-15, default 7.5 |
| `width`, `height` | int | no | 256-768, multiples of 64, default 512 |
| `seed` | int | no | -1 = random |
| `idempotency_key` | string | no | dedupes retried requests within a 24h window |

**Response — `202 Accepted`**

```json
{
  "job_id": "5b1c9e2a-3f3a-4e9a-9b1a-1a2b3c4d5e6f",
  "status": "queued",
  "created_at": "2026-07-26T10:15:00Z",
  "estimated_wait_seconds": 4
}
```

**Errors**

| Status | Code | Meaning |
|---|---|---|
| `400` | `invalid_request` | Validation failure (bad enum, out-of-range param) |
| `401` | `unauthorized` | Missing/invalid API key |
| `403` | `content_policy_violation` | Prompt rejected by pre-generation moderation check |
| `429` | `rate_limited` | Per-key or perimeter rate limit exceeded |
| `503` | `capacity_exceeded` | GPU tier at max scale and queue depth over threshold |

---

### `GET /v1/jobs/{job_id}`

Poll job status.

**Response — `200 OK`**

```json
{
  "job_id": "5b1c9e2a-3f3a-4e9a-9b1a-1a2b3c4d5e6f",
  "status": "succeeded",
  "created_at": "2026-07-26T10:15:00Z",
  "completed_at": "2026-07-26T10:15:06Z",
  "image_url": "https://images.diffusecraft.ai/5b1c9e2a.png",
  "params": {
    "model": "sd-1.5",
    "steps": 25,
    "guidance_scale": 7.5,
    "width": 512,
    "height": 512,
    "seed": 847213
  }
}
```

`status` is one of `queued`, `processing`, `succeeded`, `failed`. On `failed`, an
`error_code` field explains why (e.g. `moderation_flagged`, `generation_timeout`).

---

### `GET /v1/models`

List available model backends and their current status.

```json
{
  "models": [
    {"id": "sd-1.5", "name": "Stable Diffusion 1.5", "max_resolution": 768, "status": "available"},
    {"id": "sd-2.1", "name": "Stable Diffusion 2.1", "max_resolution": 768, "status": "available"},
    {"id": "dreamlike-photoreal", "name": "Dreamlike Photoreal 2.0", "max_resolution": 768, "status": "available"},
    {"id": "openjourney", "name": "OpenJourney", "max_resolution": 512, "status": "available"}
  ]
}
```

---

### `GET /health`

Unauthenticated liveness/readiness probe used by the ALB target group health check.
Returns `200` only if the API process can reach RDS, Redis, and SQS; otherwise `503`.

```json
{"status": "ok", "db": "ok", "cache": "ok", "queue": "ok"}
```

---

### Webhooks (optional, per-key opt-in)

Instead of polling, a key can register a webhook URL. On job completion, the worker
publishes to an SNS topic which fans out to an HTTPS webhook delivery Lambda:

```json
POST <your_webhook_url>
{
  "event": "job.succeeded",
  "job_id": "5b1c9e2a-3f3a-4e9a-9b1a-1a2b3c4d5e6f",
  "image_url": "https://images.diffusecraft.ai/5b1c9e2a.png"
}
```

Requests are signed with an HMAC-SHA256 signature in the `X-DiffuseCraft-Signature`
header (`hex(hmac_sha256(webhook_secret, raw_body))`) so receivers can verify
authenticity.

## Versioning & Deprecation

- Breaking changes are shipped under a new URI version (`/v2`) rather than mutating `/v1`.
- Deprecated fields/endpoints are announced with a minimum 90-day sunset window and a
  `Deprecation`/`Sunset` response header during that window.

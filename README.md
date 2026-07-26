# DiffuseCraft

**Open-source text-to-image generation powered by Hugging Face `diffusers` and CUDA.**

DiffuseCraft is a lightweight, self-hosted web app for generating images from
text prompts using free, pretrained latent diffusion models (Stable Diffusion
1.5 / 2.1, Dreamlike Photoreal, OpenJourney). It runs on your own GPU, or on a
**free Google Colab GPU** with a public browser URL — no paid API keys, no
proprietary models.

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/shakib79ai/DiffuseCraft/blob/main/DiffuseCraft_Colab.ipynb)
![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)
![CUDA](https://img.shields.io/badge/CUDA-enabled-76B900.svg)

---

## Features

- **Free, open-weight models** — pulled directly from the Hugging Face Hub, no API key or paid endpoint required.
- **CUDA-accelerated** — runs in float16 on GPU with attention slicing and optional `xformers` for lower VRAM use; falls back to CPU automatically if no GPU is present.
- **Web UI** — built with [Gradio](https://www.gradio.app/), served locally or via a public `*.gradio.live` link.
- **One-click Google Colab** — free T4 GPU, no local setup required.
- **Configurable generation** — model choice, sampler/scheduler, steps, CFG scale, resolution, seed, and negative prompts.
- **Multiple model backends** — swap between SD 1.5, SD 2.1, Dreamlike Photoreal, and OpenJourney from a dropdown.

## Demo

| Prompt | Output |
|---|---|
| *"A cinematic photo of a red fox in a snowy forest, golden hour lighting"* | `assets/sample_1.png` |
| *"A cyberpunk city street at night, neon reflections on wet asphalt"* | `assets/sample_2.png` |

*(Add your own generated samples to `assets/` and update this table.)*

---

## Quick Start

### Option A — Google Colab (no setup, free GPU)

1. Click the **Open in Colab** badge above (or open [`DiffuseCraft_Colab.ipynb`](DiffuseCraft_Colab.ipynb)).
2. `Runtime > Change runtime type > T4 GPU`.
3. `Runtime > Run all`.
4. Wait for the last cell to print a public URL like `https://xxxxxxx.gradio.live` — open it in any browser (desktop or mobile).

### Option B — Local machine with an NVIDIA GPU

```bash
git clone https://github.com/shakib79ai/DiffuseCraft.git
cd DiffuseCraft

python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

pip install -r requirements.txt
python app.py
```

The app launches at `http://127.0.0.1:7860` and also prints a temporary public
`gradio.live` URL (via `share=True`) so you can access it from another device.

> **No GPU?** The app detects CUDA automatically and falls back to CPU —
> generation will be slow (minutes per image) but functional.

---

## How It Works

```
┌─────────────┐      ┌──────────────────────┐      ┌───────────────────┐
│   Gradio    │ ───▶ │  diffusers pipeline   │ ───▶ │  Hugging Face Hub  │
│   Web UI    │      │  (StableDiffusion*)   │      │  pretrained weights│
└─────────────┘      └──────────┬───────────┘      └───────────────────┘
                                 │
                                 ▼
                       ┌───────────────────┐
                       │   CUDA / cuDNN     │
                       │  fp16 + attention   │
                       │     slicing         │
                       └───────────────────┘
```

1. **Prompt in** — the user enters a prompt, negative prompt, and generation settings in the Gradio UI.
2. **Model load** — the selected pipeline is downloaded (once, then cached) from the Hugging Face Hub and moved to the GPU in float16.
3. **Diffusion sampling** — the scheduler (DPM++ 2M or Euler Ancestral) iteratively denoises a latent over N steps, guided by CFG.
4. **Decode + serve** — the VAE decodes the final latent into an image, returned to the browser.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for design rationale, memory/performance notes, and extension points.

---

## Production Deployment

This repo's `app.py` + Colab notebook is the **OSS demo** — a single-process app for
one user on a local GPU or a free Colab runtime. Running DiffuseCraft as a public,
multi-tenant hosted service is a different engineering problem (async job queues,
autoscaling GPU fleets, auth, rate limiting, content moderation, HA data stores), and
that's specified in full in a dedicated production doc set:

| Document | Covers |
|---|---|
| [`docs/PRODUCTION_ARCHITECTURE.md`](docs/PRODUCTION_ARCHITECTURE.md) | Full AWS system design: VPC, ECS-on-EC2 GPU compute, async SQS job pipeline, RDS PostgreSQL, ALB, CloudFront, Route 53, CI/CD, scaling, disaster recovery |
| [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) | The production REST API contract (`POST /v1/generate`, job polling, webhooks, auth, rate limits) |
| [`docs/COST_ESTIMATE.md`](docs/COST_ESTIMATE.md) | Monthly AWS cost by tier (MVP vs. production), compute purchasing tradeoffs, cost optimization levers |
| [`docs/SECURITY.md`](docs/SECURITY.md) | IAM, network isolation, secrets/encryption, WAF, content moderation, audit logging, pre-launch security checklist |

**Architecture at a glance:** Route 53 → CloudFront + AWS WAF → ALB → FastAPI control
plane (Fargate) → Amazon SQS → GPU worker fleet (ECS-on-EC2, `g5.xlarge`, autoscaled,
Spot burst) → S3 (images, CDN-served) + RDS PostgreSQL with `pgvector` (jobs, users,
semantic prompt caching) + ElastiCache Redis (rate limiting). GPU compute is the
dominant cost driver (70-85% of spend) — see the cost doc for the SageMaker
Asynchronous Inference alternative that scales to zero for low/spiky traffic.

The root [`Dockerfile`](Dockerfile) containerizes the current demo app as a starting
point for that GPU worker image; `app.py`'s Gradio launch respects `GRADIO_SHARE=false`
and `PORT` env vars so the same code runs correctly both behind Colab's public tunnel
and behind a container orchestrator/load balancer.

---

## Project Structure

```
diffusecraft/
├── app.py                     # Gradio app: model loading, CUDA handling, generation
├── requirements.txt           # Python dependencies (local install, pins torch)
├── requirements-colab.txt     # Colab install list (torch/torchvision pinned via constraints.txt)
├── DiffuseCraft_Colab.ipynb   # One-click Colab launcher with public URL
├── Dockerfile                 # Container image for the demo app / GPU worker base
├── .dockerignore
├── docs/
│   ├── ARCHITECTURE.md            # Demo app design notes and extension points
│   ├── PRODUCTION_ARCHITECTURE.md # Full AWS production system design
│   ├── API_REFERENCE.md           # Production REST API contract
│   ├── COST_ESTIMATE.md           # AWS monthly cost estimates by tier
│   └── SECURITY.md                # Security controls and compliance checklist
├── examples/
│   └── sample_prompts.md      # Curated prompt examples
├── assets/                    # Sample output images
├── LICENSE
├── CONTRIBUTING.md
└── README.md
```

## Supported Models

| Model | Hugging Face repo | Notes |
|---|---|---|
| Stable Diffusion 1.5 | `runwayml/stable-diffusion-v1-5` | Fastest, most compatible, largest community |
| Stable Diffusion 2.1 | `stabilityai/stable-diffusion-2-1` | Higher default resolution, different aesthetic |
| Dreamlike Photoreal 2.0 | `dreamlike-art/dreamlike-photoreal-2.0` | Photorealistic fine-tune |
| OpenJourney | `prompthero/openjourney` | Midjourney-style fine-tune |

All models are downloaded on first use and cached locally by `diffusers` — no
manual download step needed.

## Configuration Reference

| Parameter | Description | Default |
|---|---|---|
| `Model` | Which pretrained pipeline to load | SD 1.5 |
| `Scheduler` | Sampling algorithm | DPM++ 2M |
| `Inference steps` | Denoising steps (higher = more detail, slower) | 25 |
| `Guidance scale (CFG)` | How closely to follow the prompt | 7.5 |
| `Width` / `Height` | Output resolution (multiples of 64) | 512 x 512 |
| `Seed` | Fixed seed for reproducibility (`-1` = random) | -1 |

## Requirements

- Python 3.9+
- NVIDIA GPU with CUDA (recommended — 6GB+ VRAM); CPU fallback supported but slow
- See [`requirements.txt`](requirements.txt) for exact package versions

## Roadmap

- [ ] Img2img and inpainting modes
- [ ] LoRA / textual inversion support
- [ ] Batch generation and prompt queues
- [ ] SDXL and SDXL-Turbo support
- [ ] Reference `server/` FastAPI implementation of [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md)
- [ ] Terraform modules for [`docs/PRODUCTION_ARCHITECTURE.md`](docs/PRODUCTION_ARCHITECTURE.md)

## Contributing

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for guidelines on setting up a dev environment, coding style, and submitting pull requests.

## License

Released under the [MIT License](LICENSE). Pretrained model weights are subject
to their own licenses (see each model's Hugging Face model card, e.g.
[CreativeML Open RAIL-M](https://huggingface.co/spaces/CompVis/stable-diffusion-license)
for Stable Diffusion).

## Acknowledgements

- [Hugging Face `diffusers`](https://github.com/huggingface/diffusers)
- [Stability AI](https://huggingface.co/stabilityai) / [RunwayML](https://huggingface.co/runwayml) for pretrained Stable Diffusion checkpoints
- [Gradio](https://www.gradio.app/) for the web UI framework

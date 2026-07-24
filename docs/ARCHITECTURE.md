# Architecture

## Overview

DiffuseCraft is intentionally a thin layer over Hugging Face `diffusers`: a
Gradio UI collects generation parameters, a single module (`app.py`) manages
pipeline lifecycle and inference, and all model weights come from the public
Hugging Face Hub. There is no backend server, database, or auth layer — the
entire app is one process.

## Component Breakdown

### `app.py`

- **Model registry** (`MODELS` dict) — maps human-readable labels to Hugging
  Face repo IDs. Adding a new model is a one-line change.
- **Pipeline cache** (`_STATE["loaded"]`) — keeps at most one pipeline
  resident in GPU memory at a time. Switching models unloads the previous
  pipeline and clears the CUDA cache before loading the next, to avoid OOM on
  small-VRAM GPUs (e.g., Colab's T4 with 16GB, or consumer 6-8GB cards).
- **Scheduler swap** — schedulers (samplers) are swapped on the *existing*
  pipeline via `from_config`, avoiding a full reload when only the sampling
  algorithm changes.
- **`generate()`** — the inference entry point. Accepts all user-tunable
  parameters, builds a seeded `torch.Generator` for reproducibility, and
  streams step progress back to the UI via a callback.

### Device Handling

```python
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32
```

- On CUDA: float16 weights, `enable_attention_slicing()` (reduces peak VRAM
  by trading a small amount of speed), and `enable_xformers_memory_efficient_attention()`
  when the `xformers` package is available (silently skipped otherwise).
- On CPU: float32 (float16 CPU kernels are slow/unsupported for many ops),
  no attention slicing needed since VRAM isn't the constraint.

This means the same `app.py` runs unmodified on a local RTX card, a free
Colab T4, or a CPU-only laptop — only performance differs.

## Memory & Performance Notes

| Setting | Effect |
|---|---|
| `enable_attention_slicing()` | Lower peak VRAM, small throughput cost |
| `xformers` memory-efficient attention | Lower VRAM + faster attention, when installed |
| float16 vs float32 | ~2x memory reduction, negligible quality loss for inference |
| Resolution (width/height) | Quadratic effect on VRAM and latency |
| Inference steps | Linear effect on latency; diminishing quality returns past ~30-40 steps for DPM++ |

On a free Colab T4 (16GB VRAM), SD 1.5 at 512x512, 25 steps, DPM++ 2M
typically completes in a few seconds after the model is loaded.

## Extension Points

- **New models**: add an entry to `MODELS` in `app.py`. Any `diffusers`-compatible
  `StableDiffusionPipeline` checkpoint on the Hub works out of the box.
- **New schedulers**: add to `SCHEDULERS`; any class from `diffusers.schedulers`
  implementing `from_config` works.
- **img2img / inpainting**: swap `StableDiffusionPipeline` for
  `StableDiffusionImg2ImgPipeline` / `StableDiffusionInpaintPipeline` behind a
  mode toggle — the model registry and device handling logic can be reused as-is.
- **SDXL**: requires `StableDiffusionXLPipeline` and typically more VRAM
  (12GB+); would need conditional handling in `get_pipeline()`.
- **Queueing / batch generation**: Gradio's `.queue()` (already enabled) can
  be extended with `concurrency_count` and a job list for multi-prompt runs.

## Why Not a Separate Backend/Frontend?

For a single-GPU, single-user (or small-audience) open-source demo, a
Gradio-only architecture minimizes moving parts: no REST API to design, no
CORS/auth to manage, and `share=True` gives a public URL for free — ideal for
Colab-based distribution. If this project grows into a multi-user hosted
service, the natural next step is to split `generate()` into a FastAPI
service behind a job queue (e.g., Celery/Redis) with the Gradio (or a
React) frontend as a thin client.

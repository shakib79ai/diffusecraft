"""
DiffuseCraft — Text-to-Image generation UI built on Hugging Face diffusers.

Loads a pretrained latent diffusion model (Stable Diffusion family) from the
Hugging Face Hub, runs inference on CUDA when available, and serves a Gradio
web UI. Designed to run identically on a local GPU machine or inside a free
Google Colab GPU runtime (see DiffuseCraft_Colab.ipynb).
"""

import gc
import os
import time
from dataclasses import dataclass

import gradio as gr
import torch
from diffusers import (
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    StableDiffusionPipeline,
)

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

MODELS = {
    "Stable Diffusion 1.5 (runwayml)": "runwayml/stable-diffusion-v1-5",
    "Stable Diffusion 2.1 (stabilityai)": "stabilityai/stable-diffusion-2-1",
    "Dreamlike Photoreal 2.0": "dreamlike-art/dreamlike-photoreal-2.0",
    "OpenJourney (Midjourney-style)": "prompthero/openjourney",
}

SCHEDULERS = {
    "DPM++ 2M (fast, high quality)": DPMSolverMultistepScheduler,
    "Euler Ancestral (creative)": EulerAncestralDiscreteScheduler,
}

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
DTYPE = torch.float16 if DEVICE == "cuda" else torch.float32

NEGATIVE_DEFAULT = (
    "blurry, low quality, distorted, deformed, disfigured, bad anatomy, "
    "watermark, signature, text, extra limbs"
)


@dataclass
class LoadedPipeline:
    repo_id: str
    pipe: StableDiffusionPipeline


_STATE = {"loaded": None}  # type: dict[str, LoadedPipeline | None]


# --------------------------------------------------------------------------
# Model loading
# --------------------------------------------------------------------------

def get_pipeline(model_label: str, scheduler_label: str) -> StableDiffusionPipeline:
    repo_id = MODELS[model_label]
    loaded = _STATE["loaded"]

    if loaded is None or loaded.repo_id != repo_id:
        if loaded is not None:
            del loaded.pipe
            gc.collect()
            if DEVICE == "cuda":
                torch.cuda.empty_cache()

        pipe = StableDiffusionPipeline.from_pretrained(
            repo_id,
            torch_dtype=DTYPE,
            safety_checker=None,
        )
        pipe = pipe.to(DEVICE)

        if DEVICE == "cuda":
            pipe.enable_attention_slicing()
            try:
                pipe.enable_xformers_memory_efficient_attention()
            except Exception:
                pass  # xformers not installed; attention slicing already covers memory

        _STATE["loaded"] = LoadedPipeline(repo_id=repo_id, pipe=pipe)
        loaded = _STATE["loaded"]

    scheduler_cls = SCHEDULERS[scheduler_label]
    loaded.pipe.scheduler = scheduler_cls.from_config(loaded.pipe.scheduler.config)
    return loaded.pipe


# --------------------------------------------------------------------------
# Inference
# --------------------------------------------------------------------------

def generate(
    prompt: str,
    negative_prompt: str,
    model_label: str,
    scheduler_label: str,
    steps: int,
    guidance_scale: float,
    width: int,
    height: int,
    seed: int,
    progress: gr.Progress = gr.Progress(),
):
    if not prompt or not prompt.strip():
        raise gr.Error("Please enter a prompt.")

    progress(0, desc=f"Loading model on {DEVICE.upper()}...")
    pipe = get_pipeline(model_label, scheduler_label)

    generator = None
    if seed is not None and seed >= 0:
        generator = torch.Generator(device=DEVICE).manual_seed(int(seed))

    def cb(step, timestep, latents):
        progress(step / max(steps, 1), desc=f"Denoising step {step}/{steps}")

    start = time.time()
    result = pipe(
        prompt=prompt,
        negative_prompt=negative_prompt or None,
        num_inference_steps=int(steps),
        guidance_scale=float(guidance_scale),
        width=int(width),
        height=int(height),
        generator=generator,
        callback=cb,
        callback_steps=1,
    )
    elapsed = time.time() - start

    image = result.images[0]
    info = (
        f"Model: {MODELS[model_label]} | Device: {DEVICE.upper()} | "
        f"Steps: {steps} | CFG: {guidance_scale} | Size: {width}x{height} | "
        f"Time: {elapsed:.1f}s"
    )
    return image, info


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------

CSS = """
#title { text-align: center; margin-bottom: 0.2em; }
#subtitle { text-align: center; color: var(--body-text-color-subdued); margin-bottom: 1.5em; }
footer { visibility: hidden; }
"""

with gr.Blocks(title="DiffuseCraft — Text to Image", css=CSS, theme=gr.themes.Soft()) as demo:
    gr.Markdown("# DiffuseCraft", elem_id="title")
    gr.Markdown(
        f"Open-source text-to-image generation with Hugging Face diffusers · "
        f"Running on **{DEVICE.upper()}**",
        elem_id="subtitle",
    )

    with gr.Row():
        with gr.Column(scale=1):
            prompt = gr.Textbox(
                label="Prompt",
                placeholder="A cinematic photo of a red fox in a snowy forest, golden hour lighting",
                lines=3,
            )
            negative_prompt = gr.Textbox(
                label="Negative prompt",
                value=NEGATIVE_DEFAULT,
                lines=2,
            )

            with gr.Row():
                model_label = gr.Dropdown(
                    choices=list(MODELS.keys()),
                    value=list(MODELS.keys())[0],
                    label="Model",
                )
                scheduler_label = gr.Dropdown(
                    choices=list(SCHEDULERS.keys()),
                    value=list(SCHEDULERS.keys())[0],
                    label="Scheduler",
                )

            with gr.Row():
                steps = gr.Slider(10, 50, value=25, step=1, label="Inference steps")
                guidance_scale = gr.Slider(1, 15, value=7.5, step=0.5, label="Guidance scale (CFG)")

            with gr.Row():
                width = gr.Slider(256, 768, value=512, step=64, label="Width")
                height = gr.Slider(256, 768, value=512, step=64, label="Height")

            seed = gr.Number(value=-1, label="Seed (-1 = random)", precision=0)

            generate_btn = gr.Button("Generate", variant="primary")

        with gr.Column(scale=1):
            output_image = gr.Image(label="Result", type="pil")
            output_info = gr.Textbox(label="Run info", interactive=False)

    generate_btn.click(
        fn=generate,
        inputs=[
            prompt,
            negative_prompt,
            model_label,
            scheduler_label,
            steps,
            guidance_scale,
            width,
            height,
            seed,
        ],
        outputs=[output_image, output_info],
    )

    gr.Examples(
        examples=[
            ["A cyberpunk city street at night, neon reflections on wet asphalt, ultra detailed"],
            ["A watercolor painting of a lighthouse on a cliff during a storm"],
            ["An astronaut riding a horse on Mars, digital art"],
            ["A cozy cabin in the woods during autumn, warm light, photorealistic"],
        ],
        inputs=[prompt],
    )


if __name__ == "__main__":
    # GRADIO_SHARE defaults on for Colab (needs the public *.gradio.live tunnel to be
    # reachable from a browser) and should be set to "false" in containerized/ALB
    # deployments, where the load balancer -- not Gradio's own tunnel -- exposes the
    # service publicly.
    share = os.environ.get("GRADIO_SHARE", "true").lower() != "false"
    demo.queue().launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7860)),
        share=share,
    )

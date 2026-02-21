# ComfyUI Workflow Templates

This directory contains workflow JSON files for ComfyUI generation.

## Files

| File | Purpose | Status |
|------|---------|--------|
| `sdxl_workflow.json` | SDXL text-to-image (text → photo) | ✅ Ready |
| `ipadapter_workflow.json` | IPAdapter img2img — face-preserving photo editing | ✅ Ready |
| `wanvideo_i2v_workflow.json` | WanVideo Image-to-Video — 10-second animation | ✅ Ready |
| `liveportrait_workflow.json` | LivePortrait (legacy, replaced by WanVideo) | ⚠️ Deprecated |

## Required Models on RunPod

### SDXL (text-to-image)
```
/workspace/ComfyUI/models/checkpoints/sd_xl_base_1.0.safetensors  (~7 GB)
```

### IPAdapter (photo editing)
```
/workspace/ComfyUI/models/ipadapter/ip-adapter-plus-face_sdxl_vit-h.bin  (~1 GB)
/workspace/ComfyUI/models/clip/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors  (~2.5 GB)
```

### WanVideo (photo animation)
```
/workspace/ComfyUI/models/diffusion_models/wan2.1_i2v_480p_14B_fp8_scaled.safetensors  (~14 GB)
/workspace/ComfyUI/models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors  (~6.3 GB)
/workspace/ComfyUI/models/vae/wan_2.1_vae.safetensors  (~330 MB)
```

## Required Custom Nodes

```bash
cd /workspace/ComfyUI/custom_nodes

# IPAdapter Plus (for photo editing)
git clone https://github.com/cubiq/ComfyUI_IPAdapter_plus.git

# WanVideo Wrapper (for photo animation)
git clone https://github.com/kijai/ComfyUI-WanVideoWrapper.git
cd ComfyUI-WanVideoWrapper && pip install -r requirements.txt && cd ..

# Video Helper Suite (for saving video output)
git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git
cd ComfyUI-VideoHelperSuite && pip install -r requirements.txt && cd ..
```

## How Image Upload Works

All workflows use the ComfyUI `/upload/image` endpoint to upload images before
referencing them by filename in the workflow JSON. Embedding base64 data directly
in the workflow is **not supported** by ComfyUI.

The `comfy_client.py` handles this automatically via `_upload_image()`.

## Workflow Node Requirements

### SDXL Workflow
- `CheckpointLoaderSimple` — model loader
- `CLIPTextEncode` (title "Positive") — positive prompt
- `CLIPTextEncode` (title "Negative") — negative prompt
- `KSampler` — sampling parameters
- `EmptyLatentImage` — dimensions
- `SaveImage` — output

### IPAdapter Workflow
- `LoadImage` — input image (filename set via `_upload_image`)
- `IPAdapterModelLoader` — IPAdapter model
- `CLIPVisionLoader` — CLIP Vision model
- `IPAdapter` — main processing node
- `CheckpointLoaderSimple` — SDXL base model
- `CLIPTextEncode` (Positive/Negative) — prompts
- `KSampler` — sampling
- `VAEDecode` — decoding
- `SaveImage` — output

### WanVideo Workflow
- `LoadImage` — input image (filename set via `_upload_image`)
- `WanVideoModelLoader` — WanVideo model
- `WanVideoTextEncode` — text encoder + prompts (node with both positive/negative)
- `WanVideoVAELoader` — VAE
- `WanVideoSampler` — sampling (`num_frames` updated per duration)
- `WanVideoVAEDecode` — decoding
- `VHS_VideoCombine` — video output (MP4, H.264)

## Troubleshooting

**"Workflow template not found"** — check that JSON files exist in `workflows/`

**"No output file found"** — verify output node (SaveImage / VHS_VideoCombine) is connected

**"Image upload failed"** — check ComfyUI is running and `/upload/image` endpoint is accessible

**Model not found** — update model filenames in JSON to match files in `/workspace/ComfyUI/models/`

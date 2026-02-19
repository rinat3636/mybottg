# ComfyUI Migration Architecture

## Overview

This document outlines the migration from Replicate API to self-hosted ComfyUI on RunPod for the Telegram bot project.

## Architecture Design

### 1. Core Components

#### A. ComfyUI Client (`services/comfy_client.py`)
- **Purpose**: Low-level API client for ComfyUI
- **Responsibilities**:
  - Submit workflow JSON to ComfyUI API
  - Poll for job completion
  - Download result files
  - Handle errors and timeouts
  - Support both image and video generation

#### B. Generation Service (`services/generation_service.py`)
- **Purpose**: Business logic layer (unchanged structure)
- **Responsibilities**:
  - Create generation records
  - Handle credit deduction
  - Mark completions/failures
  - Refund on errors

#### C. Queue Worker (`services/queue_worker.py`)
- **Purpose**: Background task processor
- **Changes**:
  - Replace `replicate_client` imports with `comfy_client`
  - Keep all queue logic intact
  - Maintain cancellation support
  - Keep refund logic unchanged

### 2. Generation Modes

#### Mode A: SDXL Image Generation
- **Input**: Text prompt + optional aspect ratio
- **Model**: SDXL (Stable Diffusion XL)
- **Resolution**: 1024x1024 (default), supports custom aspect ratios
- **VRAM**: Optimized for 20GB
- **Output**: PNG image
- **Workflow**: Text-to-image via ComfyUI workflow JSON

#### Mode B: LivePortrait Photo Animation
- **Input**: User photo (JPEG/PNG) + optional prompt
- **Model**: LivePortrait
- **Duration**: 10-20 seconds
- **Output**: MP4 video
- **Features**:
  - Face detection and tracking
  - Natural blinking animation
  - Subtle head movements
  - Identity preservation
- **Workflow**: Image-to-video via ComfyUI workflow JSON

### 3. API Flow

```
User Request (Telegram)
    ↓
Handler (bot_api/handlers/generate.py)
    ↓
Queue (Redis) ← enqueue_task()
    ↓
Queue Worker (services/queue_worker.py)
    ↓
ComfyUI Client (services/comfy_client.py)
    ↓
ComfyUI API (RunPod)
    ↓
Poll for completion
    ↓
Download result
    ↓
Send to user (Telegram)
```

### 4. ComfyUI API Integration

#### Endpoints Used:
- `POST /prompt` - Submit workflow
- `GET /history/{prompt_id}` - Check status
- `GET /view` - Download output file
- `GET /queue` - Check queue status (optional)

#### Request Flow:
1. **Submit**: POST workflow JSON with unique `client_id`
2. **Poll**: GET `/history/{prompt_id}` every 2-5 seconds
3. **Status Check**:
   - `pending` → continue polling
   - `running` → continue polling
   - `success` → download result
   - `error` → handle error
4. **Download**: GET `/view?filename={output_filename}`

### 5. Configuration

#### New Environment Variables:
```bash
# ComfyUI Configuration
COMFYUI_API_URL=https://your-pod-id.runpod.net
COMFYUI_API_PORT=8188
COMFYUI_API_KEY=optional_auth_token
COMFYUI_TIMEOUT=300  # 5 minutes max
COMFYUI_POLL_INTERVAL=3  # seconds
```

#### Updated `shared/config.py`:
- Remove `REPLICATE_API_TOKEN` requirement
- Add ComfyUI configuration fields
- Update validation logic

### 6. Workflow JSON Templates

#### SDXL Workflow Structure:
```json
{
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "seed": 12345,
      "steps": 20,
      "cfg": 7.0,
      "sampler_name": "euler",
      "scheduler": "normal",
      "denoise": 1.0,
      "model": ["4", 0],
      "positive": ["6", 0],
      "negative": ["7", 0],
      "latent_image": ["5", 0]
    }
  },
  "4": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": {
      "ckpt_name": "sdxl_model.safetensors"
    }
  }
  // ... more nodes
}
```

#### LivePortrait Workflow Structure:
```json
{
  "1": {
    "class_type": "LoadImage",
    "inputs": {
      "image": "input_photo.png"
    }
  },
  "2": {
    "class_type": "LivePortraitProcess",
    "inputs": {
      "source_image": ["1", 0],
      "expression_scale": 1.0,
      "duration_frames": 300
    }
  }
  // ... more nodes
}
```

### 7. Error Handling

#### Error Types:
1. **Connection Error**: ComfyUI unreachable
2. **Timeout Error**: Generation took too long
3. **GPU Error**: Out of memory / CUDA error
4. **Face Detection Error**: No face found (LivePortrait)
5. **Invalid Input**: Bad image format / corrupt file

#### Error Responses:
- All errors trigger credit refund
- User-friendly error messages
- Detailed logging with trace IDs
- Retry logic for transient errors

### 8. Migration Checklist

- [ ] Create `services/comfy_client.py`
- [ ] Create SDXL workflow JSON
- [ ] Create LivePortrait workflow JSON
- [ ] Update `shared/config.py` (add ComfyUI settings)
- [ ] Update `services/queue_worker.py` (replace replicate calls)
- [ ] Update `bot_api/handlers/generate.py` (if needed)
- [ ] Update `bot_api/handlers/video_generation.py` (for LivePortrait)
- [ ] Remove `services/replicate_client.py`
- [ ] Update `requirements.txt` (remove replicate, add dependencies)
- [ ] Update `.env.example` with new variables
- [ ] Test image generation flow
- [ ] Test video generation flow
- [ ] Test error handling
- [ ] Test cancellation
- [ ] Deploy to production

### 9. Backward Compatibility

**Preserved:**
- All database models unchanged
- Redis queue structure unchanged
- Payment system unchanged
- Referral system unchanged
- Admin system unchanged
- User service unchanged
- Ledger service unchanged

**Changed:**
- Only generation backend (Replicate → ComfyUI)
- Configuration variables
- Dependencies in requirements.txt

### 10. Performance Considerations

#### Timeouts:
- Image generation: 200s (current setting)
- Video generation: 400s (2x image timeout)
- ComfyUI connection: 10s
- File download: 60s

#### Polling:
- Interval: 3 seconds (configurable)
- Max polls: timeout / interval
- Exponential backoff on errors

#### Concurrency:
- Queue worker processes one task at a time (unchanged)
- Per-user generation lock (unchanged)
- Global queue size limit (unchanged)

### 11. Testing Strategy

#### Unit Tests:
- ComfyUI client connection
- Workflow JSON validation
- Error handling

#### Integration Tests:
- End-to-end image generation
- End-to-end video generation
- Cancellation during processing
- Refund on failure

#### Load Tests:
- Multiple concurrent users
- Queue backlog handling
- GPU memory management

## Next Steps

1. Obtain RunPod ComfyUI API URL and credentials
2. Verify SDXL model installation on RunPod
3. Install LivePortrait on RunPod (if not present)
4. Implement `comfy_client.py`
5. Create workflow JSON files
6. Update services and configuration
7. Test thoroughly before production deployment

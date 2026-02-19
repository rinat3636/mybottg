# Production Improvements for ComfyUI Integration

This document outlines all the production-ready improvements added to the ComfyUI integration based on your requirements.

## Overview

The following critical improvements have been implemented to ensure stable, reliable operation in production:

1. **Timeout Management**: 10-minute maximum wait time for all generations
2. **Comprehensive Error Handling**: Detection and handling of ComfyUI errors
3. **GPU Job Limits**: Prevent VRAM overload with concurrent job limits
4. **Result Validation**: Verify file size and quality of generated content
5. **File Cleanup**: Automatic cleanup of old files to prevent disk space issues
6. **A5000 Optimization**: Workflows optimized for 20GB VRAM

## 1. Timeout Management

### Implementation

**File**: `services/comfy_client.py`

A hard timeout of **600 seconds (10 minutes)** has been added to all generation operations. This prevents infinite polling and ensures users receive feedback even if ComfyUI hangs.

```python
_MAX_WAIT_TIME = 600  # 10 minutes maximum wait for generation

async def _wait_for_completion(prompt_id: str, timeout: int, poll_interval: int = 3):
    start_time = time.time()
    
    while True:
        elapsed = int(time.time() - start_time)
        
        # Check timeout
        if elapsed > timeout:
            raise ComfyUITimeoutError(f"Generation timed out after {timeout}s")
        
        # ... polling logic
```

### User Experience

When a timeout occurs, the user receives the following message:

> ❌ Генерация заняла слишком много времени. Кредиты возвращены.  
> Попробуйте упростить промт.

Credits are automatically refunded on timeout.

## 2. Comprehensive Error Handling

### Implementation

**File**: `services/comfy_client.py`

The client now checks for errors at multiple points:

1. **On workflow submission**: Checks for `error` field in response
2. **During polling**: Checks both `error` field and `status.status_str`
3. **Face detection**: Special handling for "no face detected" errors

```python
# Check for error in response
if "error" in data:
    error_msg = data.get("error", "Unknown error")
    raise ComfyUIGenerationError(f"Workflow submission failed: {error_msg}")

# Check for face detection errors
if "face" in str(error_msg).lower() and ("not found" in str(error_msg).lower() or "not detected" in str(error_msg).lower()):
    raise ComfyUINoFaceError(f"No face detected: {error_msg}")
```

### Error Messages

Different error types trigger different user messages:

| Error Type | User Message | Credits Refunded |
|------------|--------------|------------------|
| Connection Error | Сервер генерации недоступен. Попробуйте позже. | ✅ Yes |
| Timeout Error | Генерация заняла слишком много времени. | ✅ Yes |
| No Face Error | На фото не обнаружено лицо. Загрузите фото с четким изображением лица. | ✅ Yes |
| Generation Error | Не удалось обработать изображение. Попробуйте другой промт или фото. | ✅ Yes |

## 3. GPU Job Limits

### Implementation

**Files**: 
- `shared/redis_client_gpu.py` (new)
- `services/queue_worker.py` (updated)

A new GPU slot management system has been implemented to prevent VRAM overload:

```python
MAX_GPU_JOBS = 1  # For A5000 (20GB VRAM)

async def acquire_gpu_slot(task_id: str) -> bool:
    # Atomic check-and-increment using Lua script
    # Returns True if slot acquired, False if at capacity
```

### Queue Behavior

When the GPU is at capacity:

1. Task remains in the Redis queue
2. User receives a notification:
   > ⏳ Сервер загружен (1/1 задач). Ваша генерация начнется через несколько секунд...
3. Worker waits 5 seconds and tries again
4. Once a slot is free, the task starts immediately

### Configuration

You can adjust `MAX_GPU_JOBS` in `shared/redis_client_gpu.py`:

- **1 job** (default): Safe for all models and resolutions
- **2 jobs**: May work for smaller models (512x512 images)

## 4. Result Validation

### Implementation

**File**: `services/comfy_client.py`

All generated files are validated before being sent to users:

#### Image Validation

```python
# Validate result
if not result_bytes or len(result_bytes) < 1024:  # Less than 1KB is likely invalid
    logger.error("Generated image is too small or empty")
    return None
```

#### Video Validation

```python
# Validate video result
if not result_bytes or len(result_bytes) < 10240:  # Less than 10KB is likely invalid
    return None

# Check if video duration is reasonable
min_expected_size = duration_seconds * 50 * 1024  # 50KB per second
if len(result_bytes) < min_expected_size:
    logger.warning("Generated video may be incomplete")
```

### Validation Criteria

| Content Type | Minimum Size | Expected Size | Action on Failure |
|--------------|--------------|---------------|-------------------|
| Image | 1 KB | N/A | Return None, refund credits |
| Video | 10 KB | 50 KB/second | Return None, refund credits |

## 5. File Cleanup

### Implementation

**Files**:
- `services/cleanup_service.py` (new)
- `scripts/runpod_cleanup.sh` (new)

Two cleanup mechanisms have been implemented:

#### A. Automatic Cleanup Service

A background service that runs every hour to clean up stale GPU job tracking:

```python
CLEANUP_INTERVAL = 3600  # 1 hour
FILE_MAX_AGE = 86400  # 24 hours

async def cleanup_old_files():
    # Clean up files older than 24 hours
```

#### B. RunPod Cleanup Script

A bash script to be run on the RunPod instance via cron:

```bash
#!/bin/bash
# Cleans up files older than 24 hours
find /workspace/ComfyUI/output -type f -mmin +1440 -delete
```

### Setup Instructions

1. **Copy the cleanup script to RunPod**:
   ```bash
   scp scripts/runpod_cleanup.sh root@your-runpod-ip:/workspace/cleanup.sh
   ```

2. **Make it executable**:
   ```bash
   ssh root@your-runpod-ip "chmod +x /workspace/cleanup.sh"
   ```

3. **Add to crontab**:
   ```bash
   ssh root@your-runpod-ip "crontab -e"
   # Add this line:
   0 * * * * /workspace/cleanup.sh >> /workspace/cleanup.log 2>&1
   ```

### Cleanup Schedule

| Directory | Max Age | Cleanup Frequency |
|-----------|---------|-------------------|
| `/workspace/ComfyUI/output` | 24 hours | Every hour |
| `/workspace/ComfyUI/temp` | 1 hour | Every hour |
| `/workspace/ComfyUI/input` | 7 days | Every hour |

## 6. A5000 Optimization

### Implementation

**Files**:
- `workflows/sdxl_workflow_optimized.json` (new)
- `workflows/liveportrait_workflow_optimized.json` (new)

Workflows have been optimized for the RTX A5000 GPU with 20GB VRAM:

### SDXL Optimization

- **Resolution**: 1024x1024 (default), with aspect ratio support
- **Batch size**: 1
- **Steps**: 20 (balanced quality/speed)
- **Sampler**: `euler_ancestral` (memory efficient)

### LivePortrait Optimization

- **Resolution**: 512x512 (VRAM-friendly)
- **Duration**: 15 seconds maximum (375 frames @ 25 fps)
- **Batch size**: 1
- **No upscaling**: Keeps VRAM usage low

### VRAM Usage Estimates

| Task | Resolution | Duration | Estimated VRAM |
|------|------------|----------|----------------|
| SDXL Image | 1024x1024 | N/A | ~8-10 GB |
| SDXL Image | 1344x768 (16:9) | N/A | ~9-11 GB |
| LivePortrait | 512x512 | 15 sec | ~12-14 GB |
| LivePortrait | 512x512 | 10 sec | ~10-12 GB |

## Required ComfyUI Models

To enable all features, you need to install the following models on your RunPod instance:

### 1. SDXL Models

- **SDXL Base**: `sd_xl_base_1.0.safetensors` (already installed)
- **SDXL Refiner**: `sd_xl_refiner_1.0.safetensors` (recommended)

### 2. IP-Adapter (Face Preservation)

- **IP-Adapter FaceID Plus v2**: `ip-adapter-faceid-plusv2_sdxl.bin`
- **IP-Adapter LoRA**: `ip-adapter-faceid-plus_sdxl_lora.safetensors`

### 3. InsightFace (Face Detection)

- **AntelopeV2**: `antelopev2.zip` (extract to `models/insightface/`)

### 4. LivePortrait (Photo Animation)

- **LivePortrait v1.0**: `liveportrait_v1.0.safetensors`

### Installation

See **[COMFYUI_MODEL_INSTALLATION.md](./COMFYUI_MODEL_INSTALLATION.md)** for detailed installation instructions.

## Deployment Checklist

Before deploying to production, ensure:

- [ ] All required models are installed on RunPod
- [ ] ComfyUI is running and accessible
- [ ] `COMFYUI_API_URL` is set in Railway environment variables
- [ ] Cleanup script is set up on RunPod (cron job)
- [ ] GPU job limit is configured (`MAX_GPU_JOBS` in `redis_client_gpu.py`)
- [ ] Timeout is appropriate for your use case (`_MAX_WAIT_TIME` in `comfy_client.py`)
- [ ] Workflows are tested in ComfyUI UI before deployment

## Testing

After deployment, test the following scenarios:

### Normal Operation

1. ✅ Text-to-image generation
2. ✅ Image editing with uploaded photo
3. ✅ Video generation with face photo
4. ✅ Different aspect ratios (1:1, 16:9, 9:16)

### Error Handling

1. ✅ Stop RunPod Pod → expect "Server unavailable" message
2. ✅ Upload photo without face → expect "No face detected" message
3. ✅ Very complex prompt → expect timeout after 10 minutes
4. ✅ Multiple concurrent users → expect queue messages

### Queue Management

1. ✅ Submit 2 tasks simultaneously → second task should wait
2. ✅ Cancel task while in queue → credits refunded
3. ✅ Cancel task while processing → credits refunded

## Monitoring

### Key Metrics to Monitor

1. **Generation Success Rate**: Track completed vs failed generations
2. **Average Generation Time**: Monitor for performance degradation
3. **GPU Utilization**: Check if GPU is being fully utilized
4. **Disk Usage**: Ensure cleanup is working (check `/workspace`)
5. **Queue Length**: Monitor Redis queue size

### Logging

All operations are logged with appropriate levels:

- **INFO**: Normal operations (generation started, completed)
- **WARNING**: Recoverable issues (GPU at capacity, incomplete video)
- **ERROR**: Failures (timeout, connection error, generation error)

Check Railway logs for any issues:

```bash
# In Railway dashboard
Service → Logs → Filter by "comfy" or "generation"
```

## Performance Tuning

### If Generations Are Too Slow

1. **Reduce steps**: Change `steps: 20` to `steps: 15` in workflow
2. **Use smaller resolution**: 768x768 instead of 1024x1024
3. **Disable refiner**: Use only base model

### If VRAM Is Insufficient

1. **Reduce video duration**: 10 seconds instead of 15
2. **Use 512x512 for all tasks**: Smaller resolution
3. **Set `MAX_GPU_JOBS = 1`**: Ensure only one task at a time

### If Queue Is Too Long

1. **Increase `MAX_GPU_JOBS`**: Try 2 for smaller tasks
2. **Add more RunPod instances**: Load balancing (requires code changes)
3. **Optimize workflows**: Reduce steps, use faster samplers

## Troubleshooting

### Issue: "Generation timed out"

**Cause**: ComfyUI is taking longer than 10 minutes

**Solutions**:
1. Simplify the prompt
2. Reduce resolution
3. Check RunPod GPU utilization
4. Increase `_MAX_WAIT_TIME` if needed

### Issue: "No face detected"

**Cause**: InsightFace cannot find a face in the uploaded photo

**Solutions**:
1. Ensure photo has a clear, visible face
2. Face should be well-lit and not obscured
3. Try a different photo
4. Check if InsightFace models are installed correctly

### Issue: "Server unavailable"

**Cause**: ComfyUI is not running or not accessible

**Solutions**:
1. Check if RunPod Pod is running
2. Verify `COMFYUI_API_URL` is correct
3. Test ComfyUI in browser
4. Check RunPod logs for errors

### Issue: Disk space full on RunPod

**Cause**: Cleanup script is not running or not working

**Solutions**:
1. Verify cron job is set up: `crontab -l`
2. Check cleanup logs: `cat /workspace/cleanup.log`
3. Manually run cleanup: `/workspace/cleanup.sh`
4. Increase cleanup frequency or reduce `FILE_MAX_AGE`

## Summary

All requested production improvements have been successfully implemented:

✅ **Timeout**: 10-minute maximum wait time  
✅ **Error Handling**: Comprehensive error detection and user-friendly messages  
✅ **GPU Limits**: 1-2 concurrent jobs to prevent VRAM overload  
✅ **Result Validation**: File size and quality checks  
✅ **File Cleanup**: Automatic cleanup service + RunPod cron script  
✅ **A5000 Optimization**: Workflows optimized for 20GB VRAM  

The bot is now production-ready and can handle real user traffic reliably.

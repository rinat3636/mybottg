"""ComfyUI API client for image and video generation.

This client handles communication with a self-hosted ComfyUI instance on RunPod.
Supports SDXL image generation and LivePortrait video animation.

Production improvements:
- 10-minute maximum timeout for all generations
- Comprehensive error checking from ComfyUI responses
- Result validation (file size, video duration)
- Better logging and error messages
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional, Any, Dict
from pathlib import Path

import httpx

from shared.config import settings

logger = logging.getLogger(__name__)

# ComfyUI API endpoints
_PROMPT_ENDPOINT = "/prompt"
_HISTORY_ENDPOINT = "/history"
_VIEW_ENDPOINT = "/view"
_QUEUE_ENDPOINT = "/queue"

# Timeouts and polling
_CONNECTION_TIMEOUT = 10  # seconds
_DOWNLOAD_TIMEOUT = 60  # seconds
_MAX_WAIT_TIME = 600  # 10 minutes maximum wait for generation
_MAX_RETRIES = 3
_RETRY_BACKOFF = 2  # seconds


class ComfyUIError(Exception):
    """Base exception for ComfyUI client errors."""
    pass


class ComfyUIConnectionError(ComfyUIError):
    """Raised when ComfyUI is unreachable."""
    pass


class ComfyUITimeoutError(ComfyUIError):
    """Raised when generation times out."""
    pass


class ComfyUIGenerationError(ComfyUIError):
    """Raised when generation fails on ComfyUI side."""
    pass


class ComfyUINoFaceError(ComfyUIError):
    """Raised when no face is detected (LivePortrait)."""
    pass


def _get_base_url() -> str:
    """Construct base URL for ComfyUI API."""
    url = settings.COMFYUI_API_URL.rstrip("/")
    port = settings.COMFYUI_API_PORT
    
    # If URL already includes port, don't add it again
    if ":" in url.split("//")[-1]:
        return url
    
    return f"{url}:{port}"


def _get_headers() -> Dict[str, str]:
    """Get headers for ComfyUI API requests."""
    headers = {
        "Content-Type": "application/json",
    }
    
    # Add authentication if configured
    if settings.COMFYUI_API_KEY:
        headers["Authorization"] = f"Bearer {settings.COMFYUI_API_KEY}"
    
    return headers


async def _submit_workflow(workflow: Dict[str, Any], client_id: str) -> str:
    """Submit a workflow to ComfyUI and return the prompt_id.
    
    Args:
        workflow: ComfyUI workflow JSON
        client_id: Unique client identifier
        
    Returns:
        prompt_id: Unique identifier for this generation job
        
    Raises:
        ComfyUIConnectionError: If ComfyUI is unreachable
        ComfyUIError: If submission fails
    """
    base_url = _get_base_url()
    url = f"{base_url}{_PROMPT_ENDPOINT}"
    
    payload = {
        "prompt": workflow,
        "client_id": client_id,
    }
    
    try:
        async with httpx.AsyncClient(timeout=_CONNECTION_TIMEOUT) as client:
            response = await client.post(
                url,
                json=payload,
                headers=_get_headers(),
            )
            response.raise_for_status()
            
            data = response.json()
            
            # Check for error in response
            if "error" in data:
                error_msg = data.get("error", "Unknown error")
                logger.error("ComfyUI returned error on submission: %s", error_msg)
                raise ComfyUIGenerationError(f"Workflow submission failed: {error_msg}")
            
            prompt_id = data.get("prompt_id")
            
            if not prompt_id:
                raise ComfyUIError(f"No prompt_id in response: {data}")
            
            logger.info("Submitted workflow to ComfyUI: prompt_id=%s", prompt_id)
            return prompt_id
            
    except httpx.TimeoutException as exc:
        logger.error("ComfyUI connection timeout: %s", exc)
        raise ComfyUIConnectionError("ComfyUI is not responding") from exc
    
    except httpx.HTTPStatusError as exc:
        logger.error("ComfyUI HTTP error: %s - %s", exc.response.status_code, exc.response.text)
        raise ComfyUIError(f"ComfyUI returned error: {exc.response.status_code}") from exc
    
    except (ComfyUIGenerationError, ComfyUIError):
        raise
    
    except Exception as exc:
        logger.error("Failed to submit workflow to ComfyUI: %s", exc)
        raise ComfyUIConnectionError(f"Failed to connect to ComfyUI: {exc}") from exc


async def _check_status(prompt_id: str) -> Dict[str, Any]:
    """Check the status of a generation job.
    
    Args:
        prompt_id: The prompt ID returned by _submit_workflow
        
    Returns:
        Status information dictionary
        
    Raises:
        ComfyUIConnectionError: If ComfyUI is unreachable
    """
    base_url = _get_base_url()
    url = f"{base_url}{_HISTORY_ENDPOINT}/{prompt_id}"
    
    try:
        async with httpx.AsyncClient(timeout=_CONNECTION_TIMEOUT) as client:
            response = await client.get(url, headers=_get_headers())
            response.raise_for_status()
            
            data = response.json()
            return data.get(prompt_id, {})
            
    except Exception as exc:
        logger.warning("Failed to check status for prompt_id=%s: %s", prompt_id, exc)
        raise ComfyUIConnectionError(f"Failed to check status: {exc}") from exc


async def _wait_for_completion(
    prompt_id: str,
    timeout: int,
    poll_interval: int = 3,
) -> Dict[str, Any]:
    """Wait for a generation job to complete.
    
    Args:
        prompt_id: The prompt ID to wait for
        timeout: Maximum time to wait in seconds
        poll_interval: Time between status checks in seconds
        
    Returns:
        Final status dictionary with outputs
        
    Raises:
        ComfyUITimeoutError: If generation times out
        ComfyUIGenerationError: If generation fails
    """
    start_time = time.time()
    poll_count = 0
    
    logger.info(
        "Waiting for ComfyUI generation: prompt_id=%s, timeout=%ds, poll_interval=%ds",
        prompt_id, timeout, poll_interval
    )
    
    while True:
        elapsed = int(time.time() - start_time)
        
        # Check timeout
        if elapsed > timeout:
            logger.error("ComfyUI generation timed out: prompt_id=%s, timeout=%ds", prompt_id, timeout)
            raise ComfyUITimeoutError(f"Generation timed out after {timeout}s")
        
        poll_count += 1
        
        try:
            status_data = await _check_status(prompt_id)
            
            # Check if job is complete
            if status_data and "outputs" in status_data:
                logger.info(
                    "ComfyUI generation completed: prompt_id=%s, elapsed=%ds, polls=%d",
                    prompt_id, elapsed, poll_count
                )
                return status_data
            
            # Check for errors in status_data
            if status_data:
                # Check for error field
                if "error" in status_data:
                    error_msg = status_data.get("error", "Unknown error")
                    logger.error("ComfyUI generation failed: prompt_id=%s, error=%s", prompt_id, error_msg)
                    
                    # Check for specific error types
                    error_str = str(error_msg).lower()
                    if "face" in error_str and ("not found" in error_str or "not detected" in error_str or "no face" in error_str):
                        raise ComfyUINoFaceError(f"No face detected: {error_msg}")
                    
                    raise ComfyUIGenerationError(f"Generation failed: {error_msg}")
                
                # Check for status field indicating error
                status_info = status_data.get("status", {})
                if isinstance(status_info, dict):
                    status_str = status_info.get("status_str", "")
                    if status_str == "error":
                        error_msg = status_info.get("messages", [["error", "Unknown error"]])
                        logger.error("ComfyUI generation error: prompt_id=%s, error=%s", prompt_id, error_msg)
                        raise ComfyUIGenerationError(f"Generation failed: {error_msg}")
            
            # Log progress periodically
            if poll_count % 10 == 0:
                logger.info(
                    "ComfyUI generation still processing: prompt_id=%s, elapsed=%ds",
                    prompt_id, elapsed
                )
            
        except (ComfyUINoFaceError, ComfyUIGenerationError, ComfyUITimeoutError):
            # Re-raise specific errors
            raise
        
        except Exception as exc:
            logger.warning("Error checking status (will retry): %s", exc)
        
        # Wait before next poll
        await asyncio.sleep(poll_interval)


async def _download_output(filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
    """Download an output file from ComfyUI.
    
    Args:
        filename: Name of the output file
        subfolder: Subfolder within the output directory
        folder_type: Type of folder (usually "output")
        
    Returns:
        File contents as bytes
        
    Raises:
        ComfyUIError: If download fails
    """
    base_url = _get_base_url()
    
    params = {
        "filename": filename,
        "type": folder_type,
    }
    
    if subfolder:
        params["subfolder"] = subfolder
    
    url = f"{base_url}{_VIEW_ENDPOINT}"
    
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, params=params, headers=_get_headers())
            response.raise_for_status()
            
            logger.info("Downloaded output file: %s (%d bytes)", filename, len(response.content))
            return response.content
            
    except Exception as exc:
        logger.error("Failed to download output file %s: %s", filename, exc)
        raise ComfyUIError(f"Failed to download output: {exc}") from exc


def _extract_output_info(status_data: Dict[str, Any]) -> tuple[str, str, str]:
    """Extract output file information from status data.
    
    Args:
        status_data: Status dictionary from _wait_for_completion
        
    Returns:
        Tuple of (filename, subfolder, folder_type)
        
    Raises:
        ComfyUIError: If output info cannot be extracted
    """
    outputs = status_data.get("outputs", {})
    
    if not outputs:
        raise ComfyUIError("No outputs in status data")
    
    # Find the first output node with images or videos
    for node_id, node_output in outputs.items():
        # Check for images
        if "images" in node_output and node_output["images"]:
            image_info = node_output["images"][0]
            filename = image_info.get("filename", "")
            subfolder = image_info.get("subfolder", "")
            folder_type = image_info.get("type", "output")
            
            if filename:
                logger.info("Found output image: filename=%s, subfolder=%s", filename, subfolder)
                return filename, subfolder, folder_type
        
        # Check for videos (gifs)
        if "gifs" in node_output and node_output["gifs"]:
            gif_info = node_output["gifs"][0]
            filename = gif_info.get("filename", "")
            subfolder = gif_info.get("subfolder", "")
            folder_type = gif_info.get("type", "output")
            
            if filename:
                logger.info("Found output video: filename=%s, subfolder=%s", filename, subfolder)
                return filename, subfolder, folder_type
        
        # Check for videos (videos field - some nodes use this)
        if "videos" in node_output and node_output["videos"]:
            video_info = node_output["videos"][0]
            filename = video_info.get("filename", "")
            subfolder = video_info.get("subfolder", "")
            folder_type = video_info.get("type", "output")
            
            if filename:
                logger.info("Found output video: filename=%s, subfolder=%s", filename, subfolder)
                return filename, subfolder, folder_type
    
    raise ComfyUIError("No output file found in generation result")


def _load_workflow_template(name: str) -> Dict[str, Any]:
    """Load a workflow template from the workflows directory.
    
    Args:
        name: Name of the workflow file (without .json extension)
        
    Returns:
        Workflow dictionary
        
    Raises:
        ComfyUIError: If template cannot be loaded
    """
    workflow_path = Path(__file__).parent.parent / "workflows" / f"{name}.json"
    
    try:
        with open(workflow_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise ComfyUIError(f"Workflow template not found: {workflow_path}")
    except json.JSONDecodeError as exc:
        raise ComfyUIError(f"Invalid JSON in workflow template: {exc}")


def _build_sdxl_workflow(
    prompt: str,
    aspect_ratio: Optional[str] = None,
    negative_prompt: str = "",
    steps: int = 20,
    cfg: float = 7.0,
    seed: int = 0,
) -> Dict[str, Any]:
    """Build SDXL workflow with given parameters.
    
    Args:
        prompt: Text prompt
        aspect_ratio: Aspect ratio (e.g., "1:1", "16:9")
        negative_prompt: Negative prompt
        steps: Sampling steps
        cfg: CFG scale
        seed: Random seed
        
    Returns:
        Workflow dictionary ready for submission
    """
    workflow = _load_workflow_template("sdxl_workflow")
    
    # Calculate dimensions based on aspect ratio
    width, height = 1024, 1024  # Default 1:1
    
    if aspect_ratio == "16:9":
        width, height = 1344, 768
    elif aspect_ratio == "9:16":
        width, height = 768, 1344
    elif aspect_ratio == "4:3":
        width, height = 1152, 896
    elif aspect_ratio == "3:4":
        width, height = 896, 1152
    
    # Update workflow parameters
    # Node IDs depend on your workflow structure - adjust as needed
    # This is a generic example
    for node_id, node in workflow.items():
        if node.get("class_type") == "CLIPTextEncode":
            # Update prompts
            if "positive" in node.get("_meta", {}).get("title", "").lower():
                node["inputs"]["text"] = prompt
            elif "negative" in node.get("_meta", {}).get("title", "").lower():
                node["inputs"]["text"] = negative_prompt
        
        elif node.get("class_type") == "EmptyLatentImage":
            # Update dimensions
            node["inputs"]["width"] = width
            node["inputs"]["height"] = height
        
        elif node.get("class_type") == "KSampler":
            # Update sampling parameters
            node["inputs"]["steps"] = steps
            node["inputs"]["cfg"] = cfg
            node["inputs"]["seed"] = seed
    
    return workflow


def _build_liveportrait_workflow(
    image_bytes: bytes,
    prompt: str = "",
    duration_seconds: int = 10,
) -> Dict[str, Any]:
    """Build LivePortrait workflow with given parameters.
    
    Args:
        image_bytes: Input image data
        prompt: Optional animation prompt
        duration_seconds: Video duration
        
    Returns:
        Workflow dictionary ready for submission
    """
    workflow = _load_workflow_template("liveportrait_workflow")
    
    # Note: This is a placeholder implementation
    # The actual implementation depends on how your LivePortrait workflow
    # accepts input images (base64, file upload, etc.)
    
    # For now, we assume the workflow has placeholders that need to be filled
    # You'll need to adjust this based on your actual workflow structure
    
    import base64
    image_base64 = base64.b64encode(image_bytes).decode("utf-8")
    
    for node_id, node in workflow.items():
        if node.get("class_type") == "LoadImage":
            # This depends on your workflow - may need different approach
            node["inputs"]["image"] = image_base64
        
        elif "duration" in node.get("inputs", {}):
            node["inputs"]["duration"] = duration_seconds
        
        elif "frames" in node.get("inputs", {}):
            # Assuming 25 fps
            node["inputs"]["frames"] = duration_seconds * 25
    
    return workflow


async def generate_image(
    prompt: str,
    aspect_ratio: Optional[str] = None,
    negative_prompt: str = "",
    steps: int = 20,
    cfg: float = 7.0,
    seed: Optional[int] = None,
) -> Optional[bytes]:
    """Generate an image using SDXL on ComfyUI.
    
    Args:
        prompt: Text prompt for image generation
        aspect_ratio: Aspect ratio (e.g., "1:1", "16:9", "9:16")
        negative_prompt: Negative prompt (what to avoid)
        steps: Number of sampling steps
        cfg: CFG scale (classifier-free guidance)
        seed: Random seed (None for random)
        
    Returns:
        Image data as bytes, or None if generation fails
    """
    client_id = uuid.uuid4().hex
    
    # Generate random seed if not provided
    if seed is None:
        seed = int.from_bytes(uuid.uuid4().bytes[:4], byteorder="big")
    
    try:
        # Load workflow template
        workflow = _build_sdxl_workflow(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            negative_prompt=negative_prompt,
            steps=steps,
            cfg=cfg,
            seed=seed,
        )
        
        # Submit workflow
        prompt_id = await _submit_workflow(workflow, client_id)
        
        # Wait for completion with maximum timeout
        timeout = min(settings.GENERATION_TIMEOUT, _MAX_WAIT_TIME)
        status_data = await _wait_for_completion(
            prompt_id,
            timeout=timeout,
            poll_interval=settings.COMFYUI_POLL_INTERVAL,
        )
        
        # Extract output file info
        filename, subfolder, folder_type = _extract_output_info(status_data)
        
        # Download result
        result_bytes = await _download_output(filename, subfolder, folder_type)
        
        # Validate result
        if not result_bytes or len(result_bytes) < 1024:  # Less than 1KB is likely invalid
            logger.error("Generated image is too small or empty: %d bytes", len(result_bytes) if result_bytes else 0)
            return None
        
        logger.info("Image generation successful: %d bytes", len(result_bytes))
        return result_bytes
        
    except ComfyUINoFaceError:
        # Re-raise specific errors
        raise
    
    except ComfyUITimeoutError:
        logger.error("Image generation timed out")
        raise
    
    except Exception as exc:
        logger.error("Image generation failed: %s", exc)
        return None


async def generate_video(
    image_bytes: bytes,
    prompt: str = "",
    duration_seconds: int = 10,
) -> Optional[bytes]:
    """Generate a video using LivePortrait on ComfyUI.
    
    Args:
        image_bytes: Input image as bytes
        prompt: Optional text prompt for animation style
        duration_seconds: Video duration in seconds
        
    Returns:
        Video data as bytes, or None if generation fails
        
    Raises:
        ComfyUINoFaceError: If no face is detected in the image
    """
    client_id = uuid.uuid4().hex
    
    try:
        # Load workflow template
        workflow = _build_liveportrait_workflow(
            image_bytes=image_bytes,
            prompt=prompt,
            duration_seconds=duration_seconds,
        )
        
        # Submit workflow
        prompt_id = await _submit_workflow(workflow, client_id)
        
        # Wait for completion (video takes longer, but respect MAX_WAIT_TIME)
        timeout = min(settings.GENERATION_TIMEOUT * 2, _MAX_WAIT_TIME)
        status_data = await _wait_for_completion(
            prompt_id,
            timeout=timeout,
            poll_interval=settings.COMFYUI_POLL_INTERVAL,
        )
        
        # Extract output file info
        filename, subfolder, folder_type = _extract_output_info(status_data)
        
        # Download result
        result_bytes = await _download_output(filename, subfolder, folder_type)
        
        # Validate video result
        if not result_bytes or len(result_bytes) < 10240:  # Less than 10KB is likely invalid
            logger.error("Generated video is too small or empty: %d bytes", len(result_bytes) if result_bytes else 0)
            return None
        
        # Check if video duration is reasonable (at least 1 second worth of data)
        # Rough estimate: 1 second of video should be at least 50KB
        min_expected_size = duration_seconds * 50 * 1024
        if len(result_bytes) < min_expected_size:
            logger.warning(
                "Generated video may be incomplete: %d bytes (expected at least %d bytes for %ds)",
                len(result_bytes), min_expected_size, duration_seconds
            )
        
        logger.info("Video generation successful: %d bytes, %ds duration", len(result_bytes), duration_seconds)
        return result_bytes
        
    except ComfyUINoFaceError:
        # Re-raise - this is a user error
        raise
    
    except ComfyUITimeoutError:
        logger.error("Video generation timed out")
        raise
    
    except Exception as exc:
        logger.error("Video generation failed: %s", exc)
        return None


async def edit_image(
    images: list[bytes],
    prompt: str,
    aspect_ratio: Optional[str] = None,
) -> Optional[bytes]:
    """Edit an image using SDXL img2img on ComfyUI.
    
    Note: This currently uses text-to-image. For proper img2img,
    you need to create a dedicated workflow with image input support.
    
    Args:
        images: List of input images (currently only first is used for prompt context)
        prompt: Editing instructions
        aspect_ratio: Output aspect ratio
        
    Returns:
        Edited image as bytes, or None if generation fails
    """
    # For now, just use text-to-image with the prompt
    # TODO: Implement proper img2img workflow
    logger.warning("edit_image currently uses text-to-image - implement proper img2img workflow")
    return await generate_image(prompt, aspect_ratio=aspect_ratio)

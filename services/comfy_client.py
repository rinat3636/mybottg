"""ComfyUI API client for image and video generation.

This client handles communication with a self-hosted ComfyUI instance on RunPod.
Supports SDXL image generation, SDXL Inpainting (face-preserving), and WanVideo animation.

Production improvements:
- 10-minute maximum timeout for all generations
- Comprehensive error checking from ComfyUI responses
- Result validation (file size, video duration)
- Better logging and error messages
- Proper image upload via /upload/image endpoint (not base64 in JSON)
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
_UPLOAD_IMAGE_ENDPOINT = "/upload/image"

# Timeouts and polling
_CONNECTION_TIMEOUT = 60  # seconds — ComfyUI may take up to 60s to respond when loading model into VRAM
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
    """Construct base URL for ComfyUI API.

    RunPod proxy URLs (*.proxy.runpod.net) already encode the port in the
    subdomain (e.g. whckka72jkpswk-8188.proxy.runpod.net) and do NOT accept
    an explicit :port suffix — adding it breaks the connection.
    For plain IP/hostname URLs we still append the configured port.
    """
    url = settings.COMFYUI_API_URL.rstrip("/")

    # RunPod proxy: port is encoded in the subdomain, never append it
    if "proxy.runpod.net" in url:
        return url

    port = settings.COMFYUI_API_PORT

    # If URL already includes an explicit port, don't add it again
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


async def _upload_image(image_bytes: bytes, filename: str = "input_image.png") -> Optional[str]:
    """Upload an image to ComfyUI and return the server-side filename.

    ComfyUI requires images to be uploaded via /upload/image before they can
    be referenced in a workflow by name. Embedding base64 data directly in the
    workflow JSON is NOT supported.

    Args:
        image_bytes: Raw image data to upload.
        filename: Desired filename on the ComfyUI server.

    Returns:
        Server-side filename string, or None on failure.
    """
    base_url = _get_base_url()
    url = f"{base_url}{_UPLOAD_IMAGE_ENDPOINT}"

    # Build headers without Content-Type so httpx sets multipart boundary automatically
    upload_headers: Dict[str, str] = {}
    if settings.COMFYUI_API_KEY:
        upload_headers["Authorization"] = f"Bearer {settings.COMFYUI_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=_CONNECTION_TIMEOUT) as client:
            files = {"image": (filename, image_bytes, "image/png")}
            data = {"overwrite": "true"}
            response = await client.post(url, files=files, data=data, headers=upload_headers)
            response.raise_for_status()
            result = response.json()
            server_filename = result.get("name")
            if not server_filename:
                raise ComfyUIError(f"Image upload failed: 'name' not in response: {result}")
            logger.info("Image uploaded to ComfyUI: %s", server_filename)
            return server_filename
    except (ComfyUIError, ComfyUIGenerationError):
        raise
    except Exception as exc:
        logger.error("Failed to upload image to ComfyUI: %s", exc)
        return None


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

    Uses /history (without prompt_id in path) because RunPod proxy
    blocks /history/{id} with 403. We fetch all recent history and
    filter by prompt_id on the client side.

    Args:
        prompt_id: The prompt ID returned by _submit_workflow

    Returns:
        Status information dictionary

    Raises:
        ComfyUIConnectionError: If ComfyUI is unreachable
    """
    base_url = _get_base_url()
    # Use /history without path param — RunPod proxy blocks /history/{id}
    url = f"{base_url}{_HISTORY_ENDPOINT}"

    try:
        async with httpx.AsyncClient(timeout=_CONNECTION_TIMEOUT) as client:
            response = await client.get(url, headers=_get_headers())
            response.raise_for_status()

            data = response.json()
            # Filter by our prompt_id
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


def _extract_output_info(status_data: Dict[str, Any]):
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


def _build_wanvideo_workflow(
    server_filename: str,
    prompt: str = "",
    duration_seconds: int = 10,
) -> Dict[str, Any]:
    """Build WanVideo Image-to-Video workflow.

    Replaces the old _build_liveportrait_workflow. Uses wanvideo_i2v_workflow.json
    template and injects the server-side filename (obtained via _upload_image).

    Args:
        server_filename: Filename returned by _upload_image (already on ComfyUI server).
        prompt: Animation description prompt from the user.
        duration_seconds: Desired video duration in seconds (approx. 8 fps × frames).

    Returns:
        Workflow dictionary ready for submission.
    """
    workflow = _load_workflow_template("wanvideo_i2v_workflow")

    # Calculate number of frames: WanVideo runs at ~8 fps internally
    # 81 frames ≈ 10 seconds at 8 fps (standard for WanVideo i2v)
    num_frames = max(17, min(duration_seconds * 8 + 1, 161))  # clamp to [17, 161]

    for node_id, node in workflow.items():
        class_type = node.get("class_type", "")

        if class_type == "LoadImage":
            # Use the server-side filename, not raw bytes
            node["inputs"]["image"] = server_filename

        elif class_type == "WanVideoSampler":
            node["inputs"]["num_frames"] = num_frames
            # Set a random seed for variety
            node["inputs"]["seed"] = int.from_bytes(uuid.uuid4().bytes[:4], byteorder="big")

        elif class_type == "WanVideoTextEncode":
            # Only update the node that holds positive/negative prompts
            if "positive" in node["inputs"] and "negative" in node["inputs"]:
                positive_prompt = prompt if prompt else "a person moving naturally"
                node["inputs"]["positive"] = positive_prompt
                node["inputs"]["negative"] = "blurry, low quality, distorted, artifacts, watermark"

    return workflow


def _build_inpainting_workflow(
    server_filename: str,
    mask_filename: str,
    prompt: str,
    seed: int = 0,
) -> Dict[str, Any]:
    """Build SDXL Inpainting workflow for face-preserving photo editing.

    Uses sd_xl_base_1.0_inpainting_0.1.safetensors + VAEEncodeForInpaint.
    The mask defines which area to inpaint (white=edit, black=preserve).
    After inpainting, the result is composited back onto the original image
    so that unmasked areas are pixel-perfect identical to the original.

    Args:
        server_filename: Filename of the input image on ComfyUI server.
        mask_filename: Filename of the mask image on ComfyUI server.
        prompt: Editing instructions from the user.
        seed: Random seed.

    Returns:
        Workflow dictionary ready for submission.
    """
    workflow = _load_workflow_template("inpainting_workflow")

    for node_id, node in workflow.items():
        class_type = node.get("class_type", "")
        title = node.get("_meta", {}).get("title", "").lower()

        if class_type == "LoadImage":
            if "mask" in title:
                node["inputs"]["image"] = mask_filename
            else:
                node["inputs"]["image"] = server_filename

        elif class_type == "CLIPTextEncode":
            if "positive" in title:
                node["inputs"]["text"] = f"{prompt}, high quality, photorealistic, detailed, sharp"
            elif "negative" in title:
                node["inputs"]["text"] = (
                    "text, watermark, low quality, blurry, deformed, ugly, bad anatomy, "
                    "extra limbs, missing limbs, disfigured, changed face, different person, "
                    "distorted face, wrong face"
                )

        elif class_type == "KSampler":
            node["inputs"]["seed"] = seed if seed else int.from_bytes(uuid.uuid4().bytes[:4], byteorder="big")

    return workflow


def _build_ipadapter_workflow(
    server_filename: str,
    prompt: str,
    aspect_ratio: Optional[str] = None,
    seed: int = 0,
) -> Dict[str, Any]:
    """Build IPAdapter img2img workflow for face-preserving photo editing.

    Args:
        server_filename: Filename returned by _upload_image (already on ComfyUI server).
        prompt: Editing instructions from the user.
        aspect_ratio: Output aspect ratio.
        seed: Random seed.

    Returns:
        Workflow dictionary ready for submission.
    """
    workflow = _load_workflow_template("ipadapter_workflow")

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

    for node_id, node in workflow.items():
        class_type = node.get("class_type", "")

        if class_type == "LoadImage":
            node["inputs"]["image"] = server_filename

        elif class_type == "CLIPTextEncode":
            title = node.get("_meta", {}).get("title", "").lower()
            if "positive" in title:
                # Combine user prompt with quality booster
                node["inputs"]["text"] = f"{prompt}, high quality, detailed, photorealistic, sharp"
            elif "negative" in title:
                node["inputs"]["text"] = (
                    "text, watermark, low quality, blurry, deformed, ugly, bad anatomy, "
                    "extra limbs, missing limbs, disfigured, different person, different identity"
                )

        elif class_type == "EmptyLatentImage":
            node["inputs"]["width"] = width
            node["inputs"]["height"] = height

        elif class_type == "KSampler":
            node["inputs"]["seed"] = seed if seed else int.from_bytes(uuid.uuid4().bytes[:4], byteorder="big")

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
    """Generate a video using WanVideo on ComfyUI.

    Uploads the source image to ComfyUI via /upload/image, then submits the
    wanvideo_i2v_workflow with the returned server filename.

    Args:
        image_bytes: Input image as bytes
        prompt: Animation description from the user
        duration_seconds: Video duration in seconds

    Returns:
        Video data as bytes, or None if generation fails

    Raises:
        ComfyUINoFaceError: If no face is detected in the image
        ComfyUIGenerationError: If image upload or generation fails
    """
    client_id = uuid.uuid4().hex

    try:
        # Step 1: Upload image to ComfyUI server
        server_filename = await _upload_image(image_bytes)
        if not server_filename:
            raise ComfyUIGenerationError("Image upload failed — could not get server filename")

        # Step 2: Build WanVideo workflow using server filename
        workflow = _build_wanvideo_workflow(
            server_filename=server_filename,
            prompt=prompt,
            duration_seconds=duration_seconds,
        )

        # Step 3: Submit workflow
        prompt_id = await _submit_workflow(workflow, client_id)

        # Step 4: Wait for completion (video takes longer, but respect MAX_WAIT_TIME)
        timeout = min(settings.GENERATION_TIMEOUT * 2, _MAX_WAIT_TIME)
        status_data = await _wait_for_completion(
            prompt_id,
            timeout=timeout,
            poll_interval=settings.COMFYUI_POLL_INTERVAL,
        )

        # Step 5: Extract output file info
        filename, subfolder, folder_type = _extract_output_info(status_data)

        # Step 6: Download result
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
        raise

    except ComfyUITimeoutError:
        logger.error("Video generation timed out")
        raise

    except Exception as exc:
        logger.error("Video generation failed: %s", exc)
        return None


async def edit_image(
    image_bytes: bytes,
    prompt: str,
    aspect_ratio: Optional[str] = None,
) -> Optional[bytes]:
    """Edit an image using SDXL Inpainting (when face detected) or ControlNet Canny (fallback).

    Strategy:
    - If InsightFace is available and detects a face: use SDXL Inpainting with a
      precise mask (glasses/hat/background region). This preserves the face pixel-perfectly.
    - If InsightFace is NOT available (e.g., Railway container without GPU libs):
      fall back to ControlNet Canny img2img with low denoise (0.45) which preserves
      structure and face much better than full inpainting.

    Args:
        image_bytes: Input image as bytes (single image).
        prompt: Editing instructions from the user.
        aspect_ratio: Output aspect ratio.

    Returns:
        Edited image as bytes, or None if generation fails.
    """
    from services.mask_generator import generate_mask

    client_id = uuid.uuid4().hex

    try:
        # Step 1: Generate mask based on prompt
        logger.info("Generating mask for inpainting: prompt='%s'", prompt)
        mask_bytes, mask_type = generate_mask(image_bytes, prompt)
        logger.info("Mask generated: type=%s, size=%d bytes", mask_type, len(mask_bytes))

        # If mask_type is 'full', both rembg and InsightFace failed
        # Fall back to ControlNet Canny img2img with moderate denoise
        if mask_type == "full":
            logger.info(
                "All segmentation failed (mask=full), falling back to ControlNet Canny img2img"
            )
            return await _edit_with_controlnet(image_bytes, prompt, aspect_ratio, denoise=0.55)

        # Step 2: Upload input image to ComfyUI server
        server_filename = await _upload_image(image_bytes, filename="input_image.png")
        if not server_filename:
            raise ComfyUIGenerationError("Image upload failed — could not get server filename")

        # Step 3: Upload mask to ComfyUI server
        mask_filename = await _upload_image(mask_bytes, filename="mask_image.png")
        if not mask_filename:
            raise ComfyUIGenerationError("Mask upload failed — could not get server filename")

        # Step 4: Build inpainting workflow
        seed = int.from_bytes(uuid.uuid4().bytes[:4], byteorder="big")
        workflow = _build_inpainting_workflow(
            server_filename=server_filename,
            mask_filename=mask_filename,
            prompt=prompt,
            seed=seed,
        )

        # Step 5: Submit workflow
        prompt_id = await _submit_workflow(workflow, client_id)

        # Step 6: Wait for completion
        timeout = min(settings.GENERATION_TIMEOUT, _MAX_WAIT_TIME)
        status_data = await _wait_for_completion(
            prompt_id,
            timeout=timeout,
            poll_interval=settings.COMFYUI_POLL_INTERVAL,
        )

        # Step 7: Extract output file info
        filename, subfolder, folder_type = _extract_output_info(status_data)

        # Step 8: Download result
        result_bytes = await _download_output(filename, subfolder, folder_type)

        # Validate result
        if not result_bytes or len(result_bytes) < 1024:
            logger.error("Edited image is too small or empty: %d bytes", len(result_bytes) if result_bytes else 0)
            return None

        logger.info("Inpainting successful: mask_type=%s, result=%d bytes", mask_type, len(result_bytes))
        return result_bytes

    except ComfyUINoFaceError:
        raise

    except ComfyUITimeoutError:
        logger.error("Image editing timed out")
        raise

    except Exception as exc:
        logger.error("Image editing failed: %s", exc)
        return None


async def _edit_with_controlnet(
    image_bytes: bytes,
    prompt: str,
    aspect_ratio: Optional[str] = None,
    denoise: float = 0.45,
) -> Optional[bytes]:
    """Fallback: edit image using ControlNet Canny img2img with low denoise.

    Low denoise (0.45) preserves the original structure and face much better
    than the default 0.65. This is used when InsightFace is not available.

    Args:
        image_bytes: Input image as bytes.
        prompt: Editing instructions.
        aspect_ratio: Output aspect ratio.
        denoise: Denoising strength (lower = more faithful to original).

    Returns:
        Edited image as bytes, or None if generation fails.
    """
    client_id = uuid.uuid4().hex

    try:
        server_filename = await _upload_image(image_bytes, filename="input_image.png")
        if not server_filename:
            raise ComfyUIGenerationError("Image upload failed")

        seed = int.from_bytes(uuid.uuid4().bytes[:4], byteorder="big")
        workflow = _build_ipadapter_workflow(
            server_filename=server_filename,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            seed=seed,
        )

        # Override denoise to lower value for better face preservation
        for node_id, node in workflow.items():
            if node.get("class_type") == "KSampler":
                node["inputs"]["denoise"] = denoise
                break

        prompt_id = await _submit_workflow(workflow, client_id)
        timeout = min(settings.GENERATION_TIMEOUT, _MAX_WAIT_TIME)
        status_data = await _wait_for_completion(
            prompt_id,
            timeout=timeout,
            poll_interval=settings.COMFYUI_POLL_INTERVAL,
        )

        filename, subfolder, folder_type = _extract_output_info(status_data)
        result_bytes = await _download_output(filename, subfolder, folder_type)

        if not result_bytes or len(result_bytes) < 1024:
            logger.error("ControlNet fallback: result too small: %d bytes",
                         len(result_bytes) if result_bytes else 0)
            return None

        logger.info("ControlNet fallback successful: denoise=%.2f, result=%d bytes",
                    denoise, len(result_bytes))
        return result_bytes

    except (ComfyUINoFaceError, ComfyUITimeoutError):
        raise
    except Exception as exc:
        logger.error("ControlNet fallback failed: %s", exc)
        return None

"""Automatic mask generation for inpainting.

This module generates binary masks for targeted inpainting:
- Face area detection using InsightFace for face-preserving operations
- Accessory area masks (eyes/glasses region, mouth, etc.)
- Fallback to simple geometric masks when face detection fails

The mask is white (255) where inpainting should happen,
and black (0) where the original image should be preserved.
"""

from __future__ import annotations

import io
import logging
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)


def _load_image_from_bytes(image_bytes: bytes) -> Image.Image:
    """Load PIL image from bytes."""
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _image_to_bytes(image: Image.Image, format: str = "PNG") -> bytes:
    """Convert PIL image to bytes."""
    buf = io.BytesIO()
    image.save(buf, format=format)
    return buf.getvalue()


def _detect_face_landmarks(image: Image.Image) -> Optional[dict]:
    """Detect face landmarks using InsightFace.

    Returns a dict with face bounding box and key landmarks,
    or None if no face is detected.
    """
    try:
        import insightface
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(640, 640))

        img_array = np.array(image)
        faces = app.get(img_array)

        if not faces:
            logger.warning("No faces detected by InsightFace")
            return None

        # Use the largest face
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))

        return {
            "bbox": face.bbox.astype(int).tolist(),  # [x1, y1, x2, y2]
            "kps": face.kps.astype(int).tolist() if face.kps is not None else None,  # 5 keypoints
        }

    except ImportError:
        logger.warning("InsightFace not available, falling back to simple mask")
        return None
    except Exception as exc:
        logger.warning("Face detection failed: %s", exc)
        return None


def _make_glasses_mask(image: Image.Image, face_info: dict) -> Image.Image:
    """Create a mask covering the eye/glasses region of the face.

    The mask covers the area from just above the eyes to just below,
    spanning the full width of the face. This allows inpainting to add
    sunglasses while preserving the rest of the face.

    Args:
        image: Input PIL image.
        face_info: Dict with 'bbox' and optional 'kps' from InsightFace.

    Returns:
        Grayscale mask image (white = inpaint, black = preserve).
    """
    w, h = image.size
    mask = Image.new("L", (w, h), 0)  # Black = preserve everything
    draw = ImageDraw.Draw(mask)

    bbox = face_info["bbox"]  # [x1, y1, x2, y2]
    face_w = bbox[2] - bbox[0]
    face_h = bbox[3] - bbox[1]

    kps = face_info.get("kps")

    if kps and len(kps) >= 2:
        # kps[0] = right eye, kps[1] = left eye (InsightFace convention)
        eye_right = kps[0]
        eye_left = kps[1]

        eye_center_y = (eye_right[1] + eye_left[1]) // 2
        eye_center_x = (eye_right[0] + eye_left[0]) // 2

        # Glasses region: wider and taller than just eyes
        pad_x = int(face_w * 0.15)
        pad_y_top = int(face_h * 0.08)
        pad_y_bottom = int(face_h * 0.10)

        x1 = max(0, bbox[0] - pad_x)
        x2 = min(w, bbox[2] + pad_x)
        y1 = max(0, eye_center_y - pad_y_top - int(face_h * 0.05))
        y2 = min(h, eye_center_y + pad_y_bottom + int(face_h * 0.05))

    else:
        # Fallback: estimate eye region from face bbox (upper 1/3 of face)
        x1 = max(0, bbox[0] - int(face_w * 0.05))
        x2 = min(w, bbox[2] + int(face_w * 0.05))
        y1 = bbox[1] + int(face_h * 0.15)
        y2 = bbox[1] + int(face_h * 0.45)

    # Draw white rectangle for glasses region
    draw.rectangle([x1, y1, x2, y2], fill=255)

    # Feather the mask edges for smoother blending
    mask = mask.filter(ImageFilter.GaussianBlur(radius=8))

    # Re-threshold to keep it mostly binary
    mask_array = np.array(mask)
    mask_array = (mask_array > 30).astype(np.uint8) * 255
    mask = Image.fromarray(mask_array)

    logger.info("Glasses mask created: region=(%d,%d,%d,%d)", x1, y1, x2, y2)
    return mask


def _make_hat_mask(image: Image.Image, face_info: dict) -> Image.Image:
    """Create a mask covering the top of the head (for adding hats/hairstyles)."""
    w, h = image.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)

    bbox = face_info["bbox"]
    face_w = bbox[2] - bbox[0]

    # Hat region: above the face
    x1 = max(0, bbox[0] - int(face_w * 0.3))
    x2 = min(w, bbox[2] + int(face_w * 0.3))
    y1 = 0
    y2 = bbox[1] + int((bbox[3] - bbox[1]) * 0.1)  # Slightly into the face top

    draw.rectangle([x1, y1, x2, y2], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=12))

    logger.info("Hat mask created: region=(%d,%d,%d,%d)", x1, y1, x2, y2)
    return mask


def _make_background_mask(image: Image.Image, face_info: dict) -> Image.Image:
    """Create a mask covering everything EXCEPT the face (for background changes)."""
    w, h = image.size
    mask = Image.new("L", (w, h), 255)  # White = inpaint everything
    draw = ImageDraw.Draw(mask)

    bbox = face_info["bbox"]
    face_w = bbox[2] - bbox[0]
    face_h = bbox[3] - bbox[1]

    # Preserve face + some padding
    pad = int(max(face_w, face_h) * 0.15)
    x1 = max(0, bbox[0] - pad)
    x2 = min(w, bbox[2] + pad)
    y1 = max(0, bbox[1] - pad)
    y2 = min(h, bbox[3] + pad)

    draw.ellipse([x1, y1, x2, y2], fill=0)  # Black ellipse = preserve face
    mask = mask.filter(ImageFilter.GaussianBlur(radius=20))

    logger.info("Background mask created, face preserved: region=(%d,%d,%d,%d)", x1, y1, x2, y2)
    return mask


def _make_full_mask(image: Image.Image) -> Image.Image:
    """Create a full-image mask (for complete style transfer)."""
    w, h = image.size
    return Image.new("L", (w, h), 255)


def _classify_prompt(prompt: str) -> str:
    """Classify the editing prompt to determine mask type.

    Returns one of: 'glasses', 'hat', 'background', 'full'
    """
    prompt_lower = prompt.lower()

    # Glasses / eye accessories
    glasses_keywords = [
        "sunglasses", "glasses", "очки", "солнечные очки", "eyeglasses",
        "spectacles", "goggles", "shades", "очки от солнца"
    ]
    if any(kw in prompt_lower for kw in glasses_keywords):
        return "glasses"

    # Hat / head accessories
    hat_keywords = [
        "hat", "cap", "шляпа", "шапка", "кепка", "берет", "crown", "корона",
        "helmet", "шлем", "hood", "капюшон", "beret", "fedora", "baseball cap"
    ]
    if any(kw in prompt_lower for kw in hat_keywords):
        return "hat"

    # Background changes
    bg_keywords = [
        "background", "фон", "backdrop", "scene", "сцена", "setting",
        "environment", "окружение", "place", "место", "location"
    ]
    if any(kw in prompt_lower for kw in bg_keywords):
        return "background"

    # Default: full image (for style changes, clothing, etc.)
    return "full"


def generate_mask(image_bytes: bytes, prompt: str) -> Tuple[bytes, str]:
    """Generate an inpainting mask based on the prompt and image content.

    Automatically detects the face and creates an appropriate mask
    based on what the prompt is asking to change.

    Args:
        image_bytes: Input image as bytes.
        prompt: User's editing prompt (e.g., "add sunglasses").

    Returns:
        Tuple of (mask_bytes, mask_type) where mask_bytes is the mask PNG
        and mask_type is a string describing what was masked.
    """
    image = _load_image_from_bytes(image_bytes)
    mask_type = _classify_prompt(prompt)

    logger.info("Generating mask for prompt '%s', classified as: %s", prompt, mask_type)

    # Try to detect face for face-aware masking
    face_info = _detect_face_landmarks(image)

    if face_info is None:
        logger.warning("No face detected, using full-image mask")
        mask = _make_full_mask(image)
        mask_type = "full"
    elif mask_type == "glasses":
        mask = _make_glasses_mask(image, face_info)
    elif mask_type == "hat":
        mask = _make_hat_mask(image, face_info)
    elif mask_type == "background":
        mask = _make_background_mask(image, face_info)
    else:
        # For full/style changes, use background mask to preserve face
        mask = _make_background_mask(image, face_info)
        mask_type = "background_preserve_face"

    # Convert mask to RGB for ComfyUI LoadImage compatibility
    # ComfyUI's ImageToMask node reads the red channel
    mask_rgb = Image.new("RGB", image.size, (0, 0, 0))
    mask_rgb.paste(Image.merge("RGB", [mask, mask, mask]))

    mask_bytes = _image_to_bytes(mask_rgb, format="PNG")
    logger.info("Mask generated: type=%s, size=%dx%d, bytes=%d",
                mask_type, image.width, image.height, len(mask_bytes))

    return mask_bytes, mask_type

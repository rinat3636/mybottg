"""Mask generator for SDXL Inpainting.

Generates a binary mask where:
- WHITE (255) = area to be CHANGED (background)
- BLACK (0)   = area to be PRESERVED (person/subject)

Strategy:
1. Use rembg to remove background → get alpha channel = person mask
2. Invert: person=black (preserved), background=white (to inpaint)
3. Apply slight feathering for smooth edges at person boundary

This mask is passed to VAEEncodeForInpaint in ComfyUI so that
SDXL Inpainting only modifies the background while the person is untouched.

Fallback: if rembg fails, use InsightFace face bbox to estimate person area.
Last resort: full-image mask (entire image is inpainted).
"""

from __future__ import annotations

import io
import logging
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)


def _load_image_from_bytes(image_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(image_bytes)).convert("RGB")


def _image_to_bytes(image: Image.Image, format: str = "PNG") -> bytes:
    buf = io.BytesIO()
    image.save(buf, format=format)
    return buf.getvalue()


def _segment_with_rembg(image: Image.Image) -> Optional[Image.Image]:
    """Use rembg to segment person from background.

    Returns RGBA image where alpha=255 means person, alpha=0 means background.
    Returns None if rembg is not available or fails.
    """
    try:
        from rembg import remove

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        result_bytes = remove(buf.getvalue())
        result = Image.open(io.BytesIO(result_bytes)).convert("RGBA")
        logger.info("rembg segmentation successful")
        return result
    except ImportError:
        logger.warning("rembg not installed")
        return None
    except Exception as exc:
        logger.warning("rembg failed: %s", exc)
        return None


def _segment_with_insightface(image: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """Use InsightFace to detect face bounding box as fallback.

    Returns (x1, y1, x2, y2) of the face, or None.
    """
    try:
        import insightface
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(name="buffalo_l", providers=["CUDAExecutionProvider", "CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(640, 640))

        img_array = np.array(image)
        faces = app.get(img_array)

        if not faces:
            return None

        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        x1, y1, x2, y2 = face.bbox.astype(int)
        logger.info("InsightFace detected face: (%d,%d,%d,%d)", x1, y1, x2, y2)
        return max(0, x1), max(0, y1), min(image.width, x2), min(image.height, y2)

    except ImportError:
        logger.warning("InsightFace not available")
        return None
    except Exception as exc:
        logger.warning("InsightFace failed: %s", exc)
        return None


def _make_background_mask_from_rgba(image: Image.Image, rgba: Image.Image) -> Image.Image:
    """Create background mask from rembg RGBA output.

    person (alpha=255) → black (0) = preserved by inpainting
    background (alpha=0) → white (255) = inpainted/changed

    Returns RGB mask image (red channel = mask value).
    """
    alpha = np.array(rgba.split()[3])  # 0=background, 255=person

    # Invert: background becomes white (to be inpainted)
    background_mask = (255 - alpha).astype(np.uint8)

    # Slight feathering for smooth person-background boundary
    mask_img = Image.fromarray(background_mask, mode="L")
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=3))

    # Re-threshold: keep it mostly binary
    arr = np.array(mask_img)
    arr = np.where(arr > 100, 255, 0).astype(np.uint8)
    mask_img = Image.fromarray(arr, mode="L")

    # Convert to RGB (ComfyUI ImageToMask reads red channel)
    mask_rgb = Image.merge("RGB", [mask_img, mask_img, mask_img])

    white_pct = (arr == 255).sum() / arr.size * 100
    logger.info("Background mask from rembg: %.1f%% white (background)", white_pct)
    return mask_rgb


def _make_background_mask_from_face_bbox(
    image: Image.Image,
    face_bbox: Tuple[int, int, int, int]
) -> Image.Image:
    """Create background mask using face bounding box.

    Preserves face + body area (estimated), inpaints background.
    """
    w, h = image.size
    x1, y1, x2, y2 = face_bbox
    face_w = x2 - x1
    face_h = y2 - y1

    # Estimate full person area: face + body below
    # Body is roughly 3-4x face height, same width or wider
    person_x1 = max(0, x1 - int(face_w * 0.5))
    person_x2 = min(w, x2 + int(face_w * 0.5))
    person_y1 = max(0, y1 - int(face_h * 0.3))
    person_y2 = min(h, y2 + int(face_h * 3.5))

    # Create mask: white everywhere, black ellipse for person
    mask = Image.new("L", (w, h), 255)
    draw = ImageDraw.Draw(mask)
    draw.ellipse([person_x1, person_y1, person_x2, person_y2], fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=25))

    arr = np.array(mask)
    arr = np.where(arr > 128, 255, 0).astype(np.uint8)
    mask_img = Image.fromarray(arr, mode="L")
    mask_rgb = Image.merge("RGB", [mask_img, mask_img, mask_img])

    logger.info("Background mask from face bbox: person area=(%d,%d,%d,%d)", 
                person_x1, person_y1, person_x2, person_y2)
    return mask_rgb


def _make_full_mask(image: Image.Image) -> Image.Image:
    """Full white mask — entire image will be inpainted."""
    mask = Image.new("RGB", image.size, (255, 255, 255))
    return mask


def generate_mask(image_bytes: bytes, prompt: str) -> Tuple[bytes, str]:
    """Generate background mask for SDXL Inpainting.

    Always tries to segment the person and create a background-only mask
    so that SDXL Inpainting changes only the background while the person
    (face, body, hair) is preserved pixel-perfectly.

    Args:
        image_bytes: Input image as bytes.
        prompt: User's editing prompt (used for logging only).

    Returns:
        (mask_bytes, mask_type) where:
        - mask_bytes: PNG image, white=background (inpaint), black=person (preserve)
        - mask_type: 'background' | 'background_face' | 'full'
    """
    image = _load_image_from_bytes(image_bytes)
    logger.info("Generating background mask for prompt '%s', size=%s", prompt[:80], image.size)

    # Strategy 1: rembg (best — pixel-perfect person segmentation)
    rgba = _segment_with_rembg(image)
    if rgba is not None:
        mask_rgb = _make_background_mask_from_rgba(image, rgba)
        mask_bytes = _image_to_bytes(mask_rgb, format="PNG")
        logger.info("Mask generated: type=background (rembg), bytes=%d", len(mask_bytes))
        return mask_bytes, "background"

    # Strategy 2: InsightFace face bbox (fallback)
    face_bbox = _segment_with_insightface(image)
    if face_bbox is not None:
        mask_rgb = _make_background_mask_from_face_bbox(image, face_bbox)
        mask_bytes = _image_to_bytes(mask_rgb, format="PNG")
        logger.info("Mask generated: type=background_face, bytes=%d", len(mask_bytes))
        return mask_bytes, "background_face"

    # Strategy 3: Full mask (last resort — entire image inpainted)
    logger.warning("All segmentation methods failed, using full-image mask")
    mask_rgb = _make_full_mask(image)
    mask_bytes = _image_to_bytes(mask_rgb, format="PNG")
    logger.info("Mask generated: type=full, bytes=%d", len(mask_bytes))
    return mask_bytes, "full"

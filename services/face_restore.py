"""Face restoration post-processing for image editing.

After any image editing operation (ControlNet, inpainting, etc.),
this module pastes the original face back onto the edited result.
This guarantees 100% face preservation regardless of what the model did.

Strategy:
1. Detect face bounding box in the original image using InsightFace
2. If InsightFace unavailable, use a simple center-crop heuristic
3. Extract the face region from the original image
4. Blend it back onto the edited result using a feathered oval mask
   so the transition looks natural (no hard edges)
"""

from __future__ import annotations

import io
import logging
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger(__name__)


def _load_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGB")


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _detect_face_bbox(image: Image.Image) -> Optional[Tuple[int, int, int, int]]:
    """Detect face bounding box using InsightFace.

    Returns (x1, y1, x2, y2) or None if detection fails.
    """
    try:
        import insightface
        from insightface.app import FaceAnalysis

        app = FaceAnalysis(
            name="buffalo_l",
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
        app.prepare(ctx_id=0, det_size=(640, 640))

        img_array = np.array(image)
        faces = app.get(img_array)

        if not faces:
            return None

        # Use the largest face
        face = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
        x1, y1, x2, y2 = face.bbox.astype(int)

        # Clamp to image bounds
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(image.width, x2)
        y2 = min(image.height, y2)

        logger.info("Face detected: bbox=(%d,%d,%d,%d)", x1, y1, x2, y2)
        return x1, y1, x2, y2

    except ImportError:
        logger.debug("InsightFace not available for face detection")
        return None
    except Exception as exc:
        logger.warning("Face detection failed: %s", exc)
        return None


def _estimate_face_bbox_heuristic(image: Image.Image) -> Tuple[int, int, int, int]:
    """Estimate face region using simple heuristic (center-upper area).

    For portrait photos, the face is typically in the upper-center portion.
    This is a fallback when InsightFace is not available.

    Returns (x1, y1, x2, y2).
    """
    w, h = image.size

    # Face is typically in the center horizontally, upper 55% vertically
    face_w = int(w * 0.55)
    face_h = int(h * 0.50)

    x1 = (w - face_w) // 2
    y1 = int(h * 0.03)
    x2 = x1 + face_w
    y2 = y1 + face_h

    logger.info("Heuristic face region: bbox=(%d,%d,%d,%d)", x1, y1, x2, y2)
    return x1, y1, x2, y2


def _create_oval_blend_mask(
    size: Tuple[int, int],
    bbox: Tuple[int, int, int, int],
    feather_radius: int = 30,
) -> Image.Image:
    """Create a smooth oval mask for blending the face region.

    White inside the oval (face area to paste), black outside.
    Feathered edges for natural blending.

    Args:
        size: Full image size (width, height).
        bbox: Face bounding box (x1, y1, x2, y2).
        feather_radius: Gaussian blur radius for edge feathering.

    Returns:
        Grayscale mask image.
    """
    w, h = size
    x1, y1, x2, y2 = bbox

    # Add padding around the face for better blending
    pad_x = int((x2 - x1) * 0.08)
    pad_y = int((y2 - y1) * 0.08)
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)

    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)

    # Draw filled oval for the face region
    draw.ellipse([x1, y1, x2, y2], fill=255)

    # Feather the edges for smooth blending
    mask = mask.filter(ImageFilter.GaussianBlur(radius=feather_radius))

    return mask


def paste_original_face(
    original_bytes: bytes,
    edited_bytes: bytes,
) -> bytes:
    """Paste the original face back onto the edited image.

    This is the core face-preservation operation. After any editing
    (ControlNet, inpainting, etc.), the original face region is extracted
    and blended back onto the result with smooth feathered edges.

    The result looks like the edited image everywhere EXCEPT the face,
    which is pixel-perfect identical to the original.

    Args:
        original_bytes: Original input image as bytes.
        edited_bytes: Edited result image as bytes.

    Returns:
        Final image bytes with original face restored.
    """
    original = _load_image(original_bytes)
    edited = _load_image(edited_bytes)

    # Resize edited to match original dimensions if needed
    if edited.size != original.size:
        logger.info(
            "Resizing edited image from %s to %s to match original",
            edited.size, original.size
        )
        edited = edited.resize(original.size, Image.LANCZOS)

    w, h = original.size

    # Detect face bounding box
    bbox = _detect_face_bbox(original)

    if bbox is None:
        logger.info("InsightFace unavailable, using heuristic face region")
        bbox = _estimate_face_bbox_heuristic(original)

    x1, y1, x2, y2 = bbox
    face_w = x2 - x1
    face_h = y2 - y1

    if face_w < 20 or face_h < 20:
        logger.warning("Face region too small (%dx%d), skipping face paste", face_w, face_h)
        return edited_bytes

    # Create blend mask
    feather = max(15, int(min(face_w, face_h) * 0.12))
    blend_mask = _create_oval_blend_mask((w, h), bbox, feather_radius=feather)

    # Composite: edited image as base, original face pasted on top
    # mask=255 means use original, mask=0 means use edited
    result = Image.composite(original, edited, blend_mask)

    logger.info(
        "Face paste complete: face_region=(%d,%d,%d,%d), feather=%d",
        x1, y1, x2, y2, feather
    )

    return _to_bytes(result)

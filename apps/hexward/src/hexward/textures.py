"""PBR Texture Channel Lab: RGBA splitter, histogram & Normal Map DX/OGL tangent-space analyzer."""

from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
from PIL import Image


def _gray_channel_to_data_uri(arr2d: np.ndarray, max_dim: int = 256) -> str:
    img = Image.fromarray(arr2d, mode="L")
    if img.width > max_dim or img.height > max_dim:
        img.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _rgb_to_data_uri(img: Image.Image, max_dim: int = 256) -> str:
    clone = img.copy()
    if clone.width > max_dim or clone.height > max_dim:
        clone.thumbnail((max_dim, max_dim))
    buf = io.BytesIO()
    clone.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def inspect_texture_bytes(raw_bytes: bytes, filename: str = "texture.png") -> dict[str, Any]:
    """Decode image bytes into RGBA channel previews, channel stats, and normal-map sanity diagnostics."""
    with Image.open(io.BytesIO(raw_bytes)) as opened:
        rgba = opened.convert("RGBA")
        orig_mode = opened.mode
        fmt = opened.format or "PNG"

    arr = np.asarray(rgba, dtype=np.uint8)
    h, w, _ = arr.shape
    is_pow2 = (w > 0 and (w & (w - 1) == 0)) and (h > 0 and (h & (h - 1) == 0))

    channel_names = ["R", "G", "B", "A"]
    channels_meta: dict[str, dict[str, Any]] = {}
    previews: dict[str, str] = {"COMPOSITE": _rgb_to_data_uri(rgba)}

    for idx, cname in enumerate(channel_names):
        ch = arr[..., idx]
        hist, _ = np.histogram(ch, bins=16, range=(0, 256))
        channels_meta[cname] = {
            "min": int(ch.min()),
            "max": int(ch.max()),
            "mean": round(float(ch.mean()), 2),
            "std": round(float(ch.std()), 2),
            "clipped_ratio": round(float(np.mean((ch == 0) | (ch == 255))), 4),
            "hist16": [int(x) for x in hist.tolist()],
        }
        previews[cname] = _gray_channel_to_data_uri(ch)

    # Tangent-space normal-map diagnostic: B channel dominates (> 195 mean) & vectors near unit length
    b_mean = channels_meta["B"]["mean"]
    g_mean = channels_meta["G"]["mean"]
    likely_normal = b_mean >= 185.0 and 95.0 <= channels_meta["R"]["mean"] <= 165.0
    nx = (arr[..., 0].astype(np.float32) / 127.5) - 1.0
    ny = (arr[..., 1].astype(np.float32) / 127.5) - 1.0
    nz = (arr[..., 2].astype(np.float32) / 127.5) - 1.0
    mag = np.sqrt(nx * nx + ny * ny + nz * nz)

    return {
        "filename": filename,
        "format": fmt,
        "original_mode": orig_mode,
        "width": int(w),
        "height": int(h),
        "is_power_of_two": bool(is_pow2),
        "channels": channels_meta,
        "previews": previews,
        "normal_diag": {
            "likely_tangent_space": bool(likely_normal),
            "unit_length_mean": round(float(mag.mean()), 3),
            "y_polarity_hint": "OpenGL (+Y)" if g_mean >= 127.0 else "DirectX (-Y)",
        },
    }

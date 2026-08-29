"""Generate tray icons (connected/disconnected) as PNG bytes in-memory using PIL.

We draw a simple stylized snowflake / frost badge with a colored status ring:
  green  -> connected
  red    -> disconnected / error
  grey   -> idle / scanning

Returns a platform-friendly PNG bytes object.
"""

from __future__ import annotations

import io
from enum import Enum

from PIL import Image, ImageDraw


class IconState(Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    ERROR = "error"


_RING_COLOR = {
    IconState.CONNECTED: (40, 200, 90),       # green
    IconState.DISCONNECTED: (220, 70, 70),   # red
    IconState.IDLE: (160, 160, 160),         # grey
    IconState.ERROR: (240, 150, 40),         # orange
}


def render_icon(state: IconState, size: int = 64) -> bytes:
    """Render a tray icon PNG for the given state."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    ring = _RING_COLOR[state]
    cx = cy = size // 2

    # Outer status ring
    margin = 4
    bbox_ring = (margin, margin, size - margin, size - margin)
    draw.ellipse(bbox_ring, outline=ring + (255,), width=max(2, size // 20))

    # Inner frost/snowflake badge
    inner = size // 2
    half = inner // 2
    bbox_inner = (cx - half + 4, cy - half + 4, cx + half - 4, cy + half - 4)
    draw.ellipse(bbox_inner, fill=(235, 245, 255, 255), outline=(120, 160, 200, 255), width=2)

    # Simple snowflake arms
    arm_color = (60, 120, 180, 255)
    r_outer = half - 8
    r_inner = 3
    for angle_deg in (0, 60, 120, 180, 240, 300):
        import math
        a = math.radians(angle_deg)
        x1 = cx + r_inner * math.cos(a)
        y1 = cy + r_inner * math.sin(a)
        x2 = cx + r_outer * math.cos(a)
        y2 = cy + r_outer * math.sin(a)
        draw.line((x1, y1, x2, y2), fill=arm_color, width=max(2, size // 24))
        # small tip dots
        draw.ellipse((x2 - 2, y2 - 2, x2 + 2, y2 + 2), fill=arm_color)

    bio = io.BytesIO()
    img.save(bio, format="PNG")
    return bio.getvalue()

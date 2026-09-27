"""
Satellite imagery for the UI - "visual proof" next to the payout ratio.

Two jobs:
  1. Cache the upstream Agromonitoring image URLs per policy
     (satellite_cache.json). Those URLs embed the API key, so the browser
     only ever sees the backend proxy route GET /policy/{id}/satellite/image
     and the backend fetches the PNG on its behalf.
  2. Render a clearly-labelled synthetic NDVI raster for SIMULATED satellite
     readings, so the stage demo has something to show even without an
     Agromonitoring key or a cloud-free scene. It is stamped SIMULATED in
     the image itself - it must never pass for a real scene.
"""

import io
import json
import random
from pathlib import Path

import httpx

_ROOT = Path(__file__).parent.parent
CACHE_PATH = _ROOT / "satellite_cache.json"


def _load() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text() or "{}")
    except json.JSONDecodeError:
        return {}


def cache_images(policy_id: str, images: dict[str, str], captured_at: str | None) -> None:
    cache = _load()
    cache[policy_id] = {"images": images, "captured_at": captured_at}
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, indent=2))
    tmp.replace(CACHE_PATH)


def get_cached(policy_id: str) -> dict | None:
    return _load().get(policy_id)


def tile_url(template: str, lat: float, lon: float, zoom: int = 15) -> str:
    """Fill an Agromonitoring {z}/{x}/{y} tile template with the slippy-map
    tile that contains (lat, lon). Zoom 15 = ~1.2 km across at 52N."""
    import math
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_r = math.radians(lat)
    y = int((1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * n)
    return template.replace("{z}", str(zoom)).replace("{x}", str(x)).replace("{y}", str(y))


async def fetch_upstream(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


def crop_to_field(png: bytes, min_size: int = 320) -> bytes:
    """Agromonitoring tiles are masked to the polygon (transparent/white
    outside it). Crop to the field's bounding box and upscale so the field
    fills the frame instead of sitting in a corner of a mostly blank tile.
    Returns the original bytes if there's nothing to crop."""
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(png)).convert("RGBA")
        alpha = im.getchannel("A")
        bbox = alpha.getbbox()
        if bbox is None:
            # No alpha mask: fall back to "anything that isn't white".
            rgb = im.convert("RGB")
            mask = rgb.point(lambda v: 0 if v >= 250 else 255).convert("L")
            bbox = mask.getbbox()
        if bbox is None or (bbox[2] - bbox[0]) < 4 or (bbox[3] - bbox[1]) < 4:
            return png
        field = im.crop(bbox)
        scale = max(1, min_size // max(field.size))
        if scale > 1:
            field = field.resize((field.size[0] * scale, field.size[1] * scale), Image.Resampling.LANCZOS)
        out = Image.new("RGB", field.size, (11, 20, 40))
        out.paste(field, mask=field.getchannel("A"))
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001 - cropping is cosmetic
        print(f"[satellite_image] crop failed: {e!r}")
        return png


def _ndvi_colour(v: float) -> tuple[int, int, int]:
    """Brown/red for bare soil (0) through yellow (0.4) to deep green (0.8+)."""
    v = max(0.0, min(1.0, v))
    stops = [(0.0, (120, 72, 40)), (0.2, (190, 110, 50)), (0.4, (220, 200, 70)), (0.6, (110, 170, 60)), (0.8, (30, 110, 40)), (1.0, (10, 70, 30))]
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if a <= v <= b:
            t = (v - a) / (b - a) if b > a else 0
            return tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))  # type: ignore[return-value]
    return stops[-1][1]


def render_simulated_ndvi(ndvi: float, label: str = "SIMULATED", size: int = 360, seed: int = 7) -> bytes:
    """A field-shaped raster whose colours centre on `ndvi`, with spatial
    noise so it reads as imagery rather than a flat swatch, plus a legend
    and a SIMULATED stamp."""
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    cells = 24
    grid = [[ndvi + rng.gauss(0, 0.06) for _ in range(cells)] for _ in range(cells)]
    # Smooth once so patches look like crop rows / soil variation, not static.
    smooth = [[0.0] * cells for _ in range(cells)]
    for y in range(cells):
        for x in range(cells):
            n = [grid[yy][xx] for yy in range(max(0, y - 1), min(cells, y + 2)) for xx in range(max(0, x - 1), min(cells, x + 2))]
            smooth[y][x] = sum(n) / len(n)
    img = Image.new("RGB", (size, size), (14, 22, 40))
    d = ImageDraw.Draw(img)
    pad, legend_h = 14, 46
    cell = (size - 2 * pad) / cells
    for y in range(cells):
        for x in range(cells):
            x0, y0 = pad + x * cell, pad + y * cell
            d.rectangle([x0, y0, x0 + cell + 0.5, y0 + cell + 0.5], fill=_ndvi_colour(smooth[y][x]))
    # Field boundary + a couple of "tracks" so it looks like a plot.
    d.rectangle([pad, pad, size - pad, size - pad], outline=(240, 230, 200), width=2)
    for f in (0.36, 0.66):
        d.line([pad, pad + (size - 2 * pad) * f, size - pad, pad + (size - 2 * pad) * f], fill=(90, 70, 50), width=2)
    # Legend strip at the bottom.
    y0 = size - pad - legend_h
    d.rectangle([pad, y0, size - pad, size - pad], fill=(8, 15, 35))
    for i in range(100):
        x0 = pad + 8 + (size - 2 * pad - 16) * i / 100
        d.rectangle([x0, y0 + 8, x0 + (size - 2 * pad - 16) / 100 + 1, y0 + 18], fill=_ndvi_colour(i / 100))
    d.text((pad + 8, y0 + 24), "NDVI 0.0", fill=(230, 220, 200))
    d.text((size - pad - 60, y0 + 24), "1.0", fill=(230, 220, 200))
    d.text((size / 2 - 60, y0 + 24), f"mean NDVI {ndvi:.2f}", fill=(245, 240, 233))
    # Stamp.
    stamp = f"{label} - not a real scene"
    d.rectangle([pad + 6, pad + 6, pad + 12 + 7 * len(stamp), pad + 24], fill=(120, 30, 30))
    d.text((pad + 10, pad + 9), stamp, fill=(255, 235, 235))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

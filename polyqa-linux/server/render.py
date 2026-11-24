import os
from pathlib import Path

import numpy as np
from osgeo import gdal
from PIL import Image, ImageDraw

from steps import read_geometry, read_metrics

PAPER = (237, 240, 236)
WATER = (22, 38, 45)
LAND = (212, 220, 216)
INK = (16, 36, 46)
TEAL = (31, 98, 115)
MAGENTA = (171, 31, 104)

VIEWS = {
    "down": (("raster", "src"), ("raster", "down")),
    "grey": (("raster", "down"), ("raster", "grey")),
    "mask": (("raster", "grey"), ("mask", "mask")),
    "poly": (("mask", "mask"), ("outline", "poly")),
    "holes": (("outline", "poly"), ("closed", "holes")),
    "smooth": (("outline", "holes"), ("smoothed", "smooth")),
    "merge": (("smoothed", "smooth"), ("folder", None)),
}


def fit(width, height, px):
    scale = px / max(width, height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def base_image(path, px, as_mask):
    ds = gdal.Open(str(path))
    w, h = fit(ds.RasterXSize, ds.RasterYSize, px)
    band = ds.GetRasterBand(1)
    read = dict(buf_xsize=w, buf_ysize=h, resample_alg=gdal.GRIORA_NearestNeighbour)
    data = band.ReadAsArray(**read)
    nodata = band.GetNoDataValue()
    table = band.GetColorTable()
    colors = None
    if ds.RasterCount >= 3:
        colors = np.stack([data] + [ds.GetRasterBand(i).ReadAsArray(**read) for i in (2, 3)], axis=2)
    elif table is not None:
        lut = np.zeros((256, 3), dtype="uint8")
        for i in range(min(table.GetCount(), 256)):
            lut[i] = table.GetColorEntry(i)[:3]
        colors = lut[data]
    gt = ds.GetGeoTransform()
    left, top = gt[0], gt[3]
    right, bottom = left + gt[1] * ds.RasterXSize, top + gt[5] * ds.RasterYSize
    ds = None
    if as_mask:
        rgb = np.where((data != 0)[..., None], np.array(LAND), np.array(WATER))
    else:
        rgb = (colors if colors is not None else np.repeat(data[..., None], 3, axis=2)).astype("int32")
        if nodata is not None:
            rgb[data == nodata] = PAPER
    extent = (min(left, right), min(top, bottom), max(left, right), max(top, bottom))
    return Image.fromarray(rgb.astype("uint8"), "RGB"), extent


def to_pixels(ring, extent, size):
    left, bottom, right, top = extent
    w, h = size
    xs, ys = np.asarray(ring.coords).T
    px = (xs - left) / (right - left) * w
    py = (top - ys) / (top - bottom) * h
    return list(zip(px.tolist(), py.tolist()))


def draw_outline(img, geom, extent, color, width):
    d = ImageDraw.Draw(img)
    for poly in geom.geoms:
        for ring in [poly.exterior, *poly.interiors]:
            d.line(to_pixels(ring, extent, img.size), fill=color, width=width, joint="curve")


def draw_filled(img, geom, extent, color, alpha):
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for poly in getattr(geom, "geoms", [geom]):
        if poly.is_empty:
            continue
        d.polygon(to_pixels(poly.exterior, extent, img.size), fill=color + (alpha,),
                  outline=color + (255,))
    return Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")


def folder_image(paths, current, px):
    geoms = {p: read_geometry(p)[0] for p in paths}
    boxes = np.array([g.bounds for g in geoms.values()])
    extent = (boxes[:, 0].min(), boxes[:, 1].min(), boxes[:, 2].max(), boxes[:, 3].max())
    pad = max(extent[2] - extent[0], extent[3] - extent[1]) * 0.03
    extent = (extent[0] - pad, extent[1] - pad, extent[2] + pad, extent[3] + pad)
    w, h = fit(extent[2] - extent[0], extent[3] - extent[1], px)
    img = Image.new("RGB", (w, h), WATER)
    for path, geom in geoms.items():
        img = draw_filled(img, geom, extent, LAND, 255)
        draw_outline(img, geom, extent, INK if path == current else TEAL, 3 if path == current else 1)
    return img


def save(img, path):
    tmp = Path(str(path) + ".part")
    img.save(tmp, format="PNG")
    os.replace(tmp, path)


def render(work, stems, stem, step, side, px, source_path):
    kind, which = VIEWS[step][0 if side == "before" else 1]
    data = read_metrics(work, stem)
    out = work / "preview" / f"{stem}_{step}_{side}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        return out

    if kind == "folder":
        paths = []
        for other in stems:
            done = read_metrics(work, other).get("smooth") or {}
            if done.get("path"):
                paths.append(done["path"])
        if not paths:
            return None
        current = (data.get("smooth") or {}).get("path")
        save(folder_image(paths, current, px), out)
        return out

    if kind == "mask":
        img, extent = base_image(data["mask"]["path"], px, as_mask=True)
    else:
        raster = source_path if which == "src" else (data.get(which) or {}).get("path")
        if kind != "raster":
            raster = data["grey"]["path"]
        if not raster:
            return None
        img, extent = base_image(raster, px, as_mask=False)

    color, width = (TEAL, 3) if side == "after" else (INK, 2)
    if kind == "closed":
        closed = read_geometry(data["holes"]["path"])[0].difference(
            read_geometry(data["poly"]["path"])[0])
        if not closed.is_empty:
            img = draw_filled(img, closed, extent, MAGENTA, 130)
    if kind in ("outline", "closed", "smoothed"):
        draw_outline(img, read_geometry(data[which]["path"])[0], extent, color, width)
    save(img, out)
    return out


def clear(work, stem, step):
    for side in ("before", "after"):
        p = work / "preview" / f"{stem}_{step}_{side}.png"
        if p.exists():
            p.unlink()

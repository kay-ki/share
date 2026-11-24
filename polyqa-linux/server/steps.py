import json
import os
import re
from pathlib import Path

import numpy as np
import shapely
from osgeo import gdal, ogr, osr
from shapely.geometry import LinearRing, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely import wkb as shapely_wkb

gdal.UseExceptions()
ogr.UseExceptions()

STEPS = [
    ("down", "Downscale", "gdalwarp, nearest neighbour, am Zielgitter ausgerichtet, nur virtuell"),
    ("grey", "Graustufen", "Luminanz 0,2989 / 0,5870 / 0,1140 aus dem verkleinerten Raster"),
    ("mask", "Maske", "gültige Pixel gegen NoData, blockweise"),
    ("poly", "Polygonisieren", "GDALPolygonize, blockweise"),
    ("holes", "Löcher schließen", "Innenringe unter der Mindestfläche fallen weg"),
    ("smooth", "Glätten", "Douglas-Peucker im Abweichungsbudget, dann Chaikin"),
    ("merge", "Merge und GPKG", "ein Layer je Ordner, Attribute aus dem Ordnernamen"),
]
STEP_KEYS = [s[0] for s in STEPS]
SUBDIRS = {"down": "1_down", "grey": "2_grey", "mask": "3_mask",
           "poly": "4_poly", "holes": "5_holes", "smooth": "6_smooth"}
CREATE = ["TILED=YES", "BLOCKXSIZE=512", "BLOCKYSIZE=512", "COMPRESS=LZW", "BIGTIFF=IF_SAFER"]
FOLDER_STEM = "_ordner"
STRIPE = 256


def de(x, digits=3):
    return f"{x:,.{digits}f}".replace(",", " ").replace(".", ",")


def count_label(n):
    return f"{de(n / 1e6, 1)} Mio." if n >= 1e6 else de(n, 0)


def metrics_file(work, stem):
    return work / "metrics" / f"{stem}.json"


def read_metrics(work, stem):
    p = metrics_file(work, stem)
    return json.loads(p.read_text()) if p.exists() else {}


def store_metrics(work, stem, data):
    p = metrics_file(work, stem)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.part")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, p)


def write_metrics(work, stem, key, payload):
    data = read_metrics(work, stem)
    data[key] = payload
    store_metrics(work, stem, data)


def step_output(work, step, stem):
    suffix = {"down": ".vrt", "grey": ".tif", "mask": ".tif"}.get(step, ".gpkg")
    out = work / SUBDIRS[step] / f"{stem}{suffix}"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def result_path(work, stem, step):
    data = read_metrics(work, stem).get(step)
    if not data or not data.get("path"):
        raise RuntimeError("der Schritt davor liegt für dieses Raster nicht vor")
    return data["path"]


def stripes(width, height):
    for y in range(0, height, STRIPE):
        yield 0, y, width, min(STRIPE, height - y)


def spatial_ref(ds):
    if not ds.GetProjection():
        return None
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ds.GetProjection())
    return srs


def crs_label(srs):
    if srs is None:
        return "keins"
    name, code = srs.GetAuthorityName(None), srs.GetAuthorityCode(None)
    return f"{name}:{code}" if name and code else (srs.GetName() or "ohne Code")


def pixel_size(ds):
    gt = ds.GetGeoTransform()
    return abs(gt[1]), abs(gt[5])


def bounds(ds):
    gt = ds.GetGeoTransform()
    left, top = gt[0], gt[3]
    right = left + gt[1] * ds.RasterXSize
    bottom = top + gt[5] * ds.RasterYSize
    return min(left, right), min(top, bottom), max(left, right), max(top, bottom)


def create_byte(path, like, nodata):
    driver = gdal.GetDriverByName("GTiff")
    out = driver.Create(str(path), like.RasterXSize, like.RasterYSize, 1, gdal.GDT_Byte, CREATE)
    out.SetGeoTransform(like.GetGeoTransform())
    out.SetProjection(like.GetProjection())
    if nodata is not None:
        out.GetRasterBand(1).SetNoDataValue(float(nodata))
    return out


def alpha_band(ds):
    for i in range(1, ds.RasterCount + 1):
        if ds.GetRasterBand(i).GetColorInterpretation() == gdal.GCI_AlphaBand:
            return i
    return None


def color_lut(band):
    table = band.GetColorTable()
    if table is None:
        return None
    lut = np.zeros((256, 3), dtype="float32")
    for i in range(min(table.GetCount(), 256)):
        lut[i] = table.GetColorEntry(i)[:3]
    return lut


def down(src_path, work, stem, target_res, crs_fallback):
    ds = gdal.Open(str(src_path))
    if ds.GetGeoTransform(can_return_null=True) is None:
        raise RuntimeError("keine Georeferenz – fehlt die World-Datei (.pgw, .wld, .tfw)?")
    srs = spatial_ref(ds)
    substitute = srs is None and bool(crs_fallback)
    if substitute:
        srs = osr.SpatialReference()
        srs.SetFromUserInput(str(crs_fallback))
    label = crs_label(srs)
    if srs is None or not srs.IsProjected():
        raise RuntimeError(f"CRS ist nicht projiziert ({label}) – unter „Parameter“ einen CRS-Ersatz eintragen")
    res = pixel_size(ds)[0]
    size_before = (ds.RasterXSize, ds.RasterYSize)
    unit_name = srs.GetLinearUnitsName()
    goal = target_res / srs.GetLinearUnits()
    coarse = res >= goal * 0.999
    out_path = str(step_output(work, "down", stem))
    wkt = srs.ExportToWkt()
    if coarse:
        gdal.Translate(out_path, ds, format="VRT", outputSRS=wkt)
    else:
        nodata = ds.GetRasterBand(1).GetNoDataValue()
        gdal.Warp(out_path, ds, format="VRT", xRes=goal, yRes=goal, resampleAlg="near",
                  targetAlignedPixels=True, srcSRS=wkt, dstSRS=wkt,
                  srcNodata=nodata, dstNodata=nodata, overviewLevel="NONE")
    ds = None
    done = gdal.Open(out_path)
    new_res = pixel_size(done)[0]
    size_after = (done.RasterXSize, done.RasterYSize)
    done = None
    gauges = [["Pixelgröße", f"{de(new_res, 3)} {unit_name}", "ok"],
              ["Kein Upscale", "nichts zu tun" if coarse else f"{de(res, 2)} → {de(new_res, 2)}", "ok"],
              ["CRS", f"{label}, Ersatz" if substitute else label, "ok"]]
    return {"path": out_path, "flag": False, "gauges": gauges,
            "before": f"{de(res, 2)} m, {size_before[0]} × {size_before[1]} px",
            "after": f"{de(new_res, 2)} m, {size_after[0]} × {size_after[1]} px"}


def grey(work, stem, nodata_fallback):
    ds = gdal.Open(result_path(work, stem, "down"))
    bands, alpha = ds.RasterCount, alpha_band(ds)
    first = ds.GetRasterBand(1)
    lut = color_lut(first) if bands <= 2 else None
    file_nodata = first.GetNoDataValue()
    nodata = file_nodata if file_nodata is not None else nodata_fallback
    colors = 3 if bands >= 3 else 1
    if nodata is None and not alpha:
        out_nodata = None
    elif lut is not None or nodata is None:
        out_nodata = 0
    else:
        out_nodata = int(nodata)
    out_path = str(step_output(work, "grey", stem))
    out = create_byte(out_path, ds, out_nodata)
    band = out.GetRasterBand(1)
    nudged = 0
    for x, y, w, h in stripes(ds.RasterXSize, ds.RasterYSize):
        raw = [ds.GetRasterBand(i).ReadAsArray(x, y, w, h) for i in range(1, colors + 1)]
        invalid = np.zeros((h, w), dtype=bool)
        if nodata is not None:
            invalid = np.all([r == nodata for r in raw], axis=0)
        if alpha:
            invalid |= ds.GetRasterBand(alpha).ReadAsArray(x, y, w, h) == 0
        if lut is not None:
            rgb = lut[raw[0]].transpose(2, 0, 1)
        elif colors == 3:
            rgb = np.stack(raw).astype("float32")
        else:
            rgb = None
        if rgb is None:
            g = np.clip(raw[0], 0, 255).astype("uint8")
        else:
            g = np.clip(np.rint(0.2989 * rgb[0] + 0.5870 * rgb[1] + 0.1140 * rgb[2]), 0, 255).astype("uint8")
        if out_nodata is not None:
            # a valid pixel whose grey value equals NoData would turn into a hole
            clash = ~invalid & (g == out_nodata)
            nudged += int(clash.sum())
            g[clash] = 1 if out_nodata == 0 else out_nodata - 1
            g[invalid] = out_nodata
        band.WriteArray(g, x, y)
    band.FlushCache()
    res = pixel_size(ds)[0]
    out, ds = None, None
    if alpha:
        source = "Alphakanal" + (f" und {de(nodata, 0)}" if nodata is not None else "")
    elif nodata is None:
        source = "nicht gesetzt"
    else:
        source = de(nodata, 0) + (", Ersatz" if file_nodata is None else "")
    gauges = [["Bänder", f"{bands} → 1" + (", Palette" if lut is not None else ""), "ok"],
              ["NoData aus", source, "flag" if out_nodata is None else "ok"],
              ["Grau = NoData, verschoben", count_label(nudged), "ok"]]
    return {"path": out_path, "flag": out_nodata is None, "gauges": gauges,
            "before": (f"{bands} Band, " if bands == 1 else f"{bands} Bänder, ") + f"{de(res, 2)} m",
            "after": f"1 Band, NoData {out_nodata if out_nodata is not None else 'keiner'}"}


def mask(work, stem):
    ds = gdal.Open(result_path(work, stem, "grey"))
    out_path = str(step_output(work, "mask", stem))
    nodata = ds.GetRasterBand(1).GetNoDataValue()
    out = create_byte(out_path, ds, None)
    band_out, band_in = out.GetRasterBand(1), ds.GetRasterBand(1)
    hist = np.zeros(256, dtype="int64")
    valid = 0
    for x, y, w, h in stripes(ds.RasterXSize, ds.RasterYSize):
        block = band_in.ReadAsArray(x, y, w, h)
        if block.dtype == np.uint8:
            hist += np.bincount(block.ravel(), minlength=256)
        m = np.ones(block.shape, dtype="uint8") if nodata is None else (block != nodata).astype("uint8")
        valid += int(m.sum())
        band_out.WriteArray(m, x, y)
    band_out.FlushCache()
    total = ds.RasterXSize * ds.RasterYSize
    px = pixel_size(ds)
    area = px[0] * px[1]
    out, ds = None, None
    share = valid / total if total else 0.0
    plateaus = int(((hist / max(hist.sum(), 1)) > 0.05).sum()) if hist.sum() else 0
    bad_share = share <= 0.001 or share >= 0.999
    gauges = [["Gültige Pixel", f"{de(share * 100, 1)} %", "flag" if bad_share else "ok"],
              ["NoData-Wert", "nicht gesetzt" if nodata is None else de(nodata, 0),
               "flag" if nodata is None else "ok"],
              ["Werte über 5 % Anteil", str(plateaus), "flag" if plateaus > 2 else "ok"]]
    return {"path": out_path, "flag": bad_share or nodata is None or plateaus > 2,
            "gauges": gauges, "valid_px": valid, "px_area": area,
            "before": "Graustufen",
            "after": f"Binärmaske, {count_label(valid)} gültige Pixel"}


def write_geometry(path, layer_name, srs, geom, props):
    if Path(path).exists():
        Path(path).unlink()
    source = ogr.GetDriverByName("GPKG").CreateDataSource(str(path))
    layer = source.CreateLayer(layer_name, srs, ogr.wkbMultiPolygon)
    for key in props:
        layer.CreateField(ogr.FieldDefn(key, ogr.OFTString))
    feature = ogr.Feature(layer.GetLayerDefn())
    for key, value in props.items():
        feature.SetField(key, value)
    feature.SetGeometry(ogr.CreateGeometryFromWkb(geom.wkb))
    layer.CreateFeature(feature)
    feature, layer, source = None, None, None


def read_geometry(path):
    source = ogr.Open(str(path))
    layer = source.GetLayer()
    srs = layer.GetSpatialRef().Clone()
    geoms = [shapely_wkb.loads(bytes(f.GetGeometryRef().ExportToWkb())) for f in layer]
    source = None
    parts = []
    for geom in geoms:
        parts.extend(geom.geoms if geom.geom_type == "MultiPolygon" else [geom])
    return MultiPolygon(parts), srs


def polygonize(work, stem, connectivity):
    src = result_path(work, stem, "mask")
    ds = gdal.Open(src)
    band = ds.GetRasterBand(1)
    srs = spatial_ref(ds)
    raw_path = step_output(work, "poly", stem).with_name(f"{stem}_roh.gpkg")
    if raw_path.exists():
        raw_path.unlink()
    raw = ogr.GetDriverByName("GPKG").CreateDataSource(str(raw_path))
    layer = raw.CreateLayer("roh", srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("DN", ogr.OFTInteger))
    options = ["8CONNECTED=YES"] if int(connectivity) == 8 else []
    gdal.Polygonize(band, band, layer, 0, options)
    raw, layer = None, None

    source = ogr.Open(str(raw_path))
    parts = [shapely_wkb.loads(bytes(f.GetGeometryRef().ExportToWkb()))
             for f in source.GetLayer() if f.GetField("DN") == 1]
    source = None
    if not parts:
        raise RuntimeError("die Maske enthält kein einziges gültiges Pixel")
    geom = unary_union(parts) if len(parts) > 1 else parts[0]
    if geom.geom_type == "Polygon":
        geom = MultiPolygon([geom])
    out_path = str(step_output(work, "poly", stem))
    write_geometry(out_path, stem, srs, geom, {"raster": stem})

    left, bottom, right, top = bounds(ds)
    ds = None
    box = Polygon([(left, bottom), (right, bottom), (right, top), (left, top)])
    outside = geom.difference(box).area / geom.area * 100 if geom.area else 0.0
    md = read_metrics(work, stem)["mask"]
    ratio = geom.area / (md["valid_px"] * md["px_area"]) if md["valid_px"] else 0.0
    lat, lon = to_wgs84(srs, geom.centroid.x, geom.centroid.y)
    holes = sum(len(p.interiors) for p in geom.geoms)
    off = not 0.98 <= ratio <= 1.02
    gauges = [["Fläche ÷ Pixelfläche", de(ratio), "flag" if off else "ok"],
              ["Fläche außerhalb des Rasters", f"{de(outside, 1)} %", "flag" if outside > 0.1 else "ok"],
              ["Mitte in WGS 84", f"{de(lat, 4)} N  {de(lon, 4)} E", "ok"]]
    return {"path": out_path, "flag": off or outside > 0.1, "gauges": gauges,
            "before": "Binärmaske",
            "after": f"{len(geom.geoms)} Teil(e), {holes} Löcher"}


def to_wgs84(srs, x, y):
    target = osr.SpatialReference()
    target.ImportFromEPSG(4326)
    try:
        target.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        source = srs.Clone()
        source.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except AttributeError:
        source = srs
    point = osr.CoordinateTransformation(source, target).TransformPoint(x, y)
    return point[1], point[0]


def close_holes(work, stem, min_hole_area):
    geom, srs = read_geometry(result_path(work, stem, "poly"))
    kept, closed, smallest = [], 0, None
    for poly in geom.geoms:
        rings = []
        for ring in poly.interiors:
            area = Polygon(ring).area
            if area >= min_hole_area:
                rings.append(ring)
                smallest = area if smallest is None else min(smallest, area)
            else:
                closed += 1
        kept.append(Polygon(poly.exterior, rings))
    new = MultiPolygon(kept)
    gain = (new.area / geom.area - 1) * 100 if geom.area else 0.0
    out_path = str(step_output(work, "holes", stem))
    write_geometry(out_path, stem, srs, new, {"raster": stem})
    remaining = sum(len(p.interiors) for p in new.geoms)
    before_holes = sum(len(p.interiors) for p in geom.geoms)
    gauges = [["Kleinstes Loch danach", "keine Löcher mehr" if smallest is None else f"{de(smallest, 0)} m²",
               "ok" if smallest is None or smallest >= min_hole_area else "flag"],
              ["Geschlossen", f"{closed} von {before_holes}", "ok"],
              ["Flächenzuwachs", f"+{de(gain, 2)} %", "flag" if gain > 5 else "ok"]]
    return {"path": out_path, "flag": gain > 5, "gauges": gauges,
            "before": f"{before_holes} Löcher",
            "after": f"{remaining} {'Loch' if remaining == 1 else 'Löcher'}, {closed} geschlossen"}


def dp(ring, tol):
    line = LinearRing(ring).simplify(tol, preserve_topology=False)
    return line if len(line.coords) >= 4 else LinearRing(ring)


def chaikin(ring, iterations):
    pts = np.asarray(ring.coords[:-1], dtype="float64")
    for _ in range(iterations):
        nxt = np.roll(pts, -1, axis=0)
        out = np.empty((len(pts) * 2, 2))
        out[0::2] = 0.75 * pts + 0.25 * nxt
        out[1::2] = 0.25 * pts + 0.75 * nxt
        pts = out
    return LinearRing(np.vstack([pts, pts[:1]]))


def one_way(points_of, line):
    c = np.asarray(line.coords)
    tree = shapely.STRtree(shapely.linestrings(np.stack([c[:-1], c[1:]], axis=1)))
    _, dist = tree.query_nearest(shapely.points(np.asarray(points_of.coords)), return_distance=True)
    return float(dist.max())


def deviation(a, b):
    return max(one_way(a, b), one_way(b, a))


def smooth_ring(ring, max_deviation, iterations):
    for its in range(iterations, 0, -1):
        lo, hi, best = 0.0, max_deviation, None
        for _ in range(8):
            mid = (lo + hi) / 2
            # Chaikin cuts corners by a quarter of the edge length, so long straight edges blow the budget
            cand = chaikin(LinearRing(shapely.segmentize(dp(ring, mid), 2 * max_deviation)), its)
            if cand.is_valid and deviation(ring, cand) <= max_deviation:
                best, lo = cand, mid
            else:
                hi = mid
        if best is not None:
            return best, True
    return ring, False


def right_angle_share(geom):
    hits = total = 0
    for poly in geom.geoms:
        for ring in [poly.exterior, *poly.interiors]:
            p = np.asarray(ring.coords[:-1])
            if len(p) < 3:
                continue
            a = np.roll(p, 1, axis=0) - p
            b = np.roll(p, -1, axis=0) - p
            na, nb = np.linalg.norm(a, axis=1), np.linalg.norm(b, axis=1)
            ok = (na > 0) & (nb > 0)
            cos = np.zeros(len(p))
            cos[ok] = (a[ok, 0] * b[ok, 0] + a[ok, 1] * b[ok, 1]) / (na[ok] * nb[ok])
            ang = np.degrees(np.arccos(np.clip(cos, -1, 1)))
            hits += int((np.abs(ang - 90) < 5).sum())
            total += len(p)
    return hits / total * 100 if total else 0.0


def vertex_count(geom):
    return sum(len(p.exterior.coords) + sum(len(r.coords) for r in p.interiors) for p in geom.geoms)


def smooth(work, stem, max_deviation, iterations):
    geom, srs = read_geometry(result_path(work, stem, "holes"))
    result, reverted = [], 0
    for poly in geom.geoms:
        ext, ok = smooth_ring(poly.exterior, max_deviation, iterations)
        if not ok or not Polygon(ext, poly.interiors).is_valid:
            ext, reverted = poly.exterior, reverted + 1
        shell = Polygon(ext)
        rings = []
        for ring in poly.interiors:
            cand, ok = smooth_ring(ring, max_deviation, iterations)
            hole = Polygon(cand)
            clash = any(hole.intersects(Polygon(r)) for r in rings)
            if not ok or not cand.is_valid or not shell.contains(hole) or clash:
                cand, reverted = ring, reverted + 1
            rings.append(cand)
        result.append(Polygon(ext, rings))
    accepted = []
    for original, cand in zip(geom.geoms, result):
        if any(cand.intersection(a).area > 0 for a in accepted):
            cand, reverted = original, reverted + 1
        accepted.append(cand)
    new = MultiPolygon(accepted)
    out_path = str(step_output(work, "smooth", stem))
    write_geometry(out_path, stem, srs, new, {"raster": stem})
    dev = max(deviation(geom.geoms[i].exterior, new.geoms[i].exterior)
              for i in range(len(new.geoms)))
    before_ang, after_ang = right_angle_share(geom), right_angle_share(new)
    stairs = after_ang > 20
    gauges = [["Größte Abweichung", f"{de(dev, 2)} von {de(max_deviation, 2)} m",
               "flag" if dev > max_deviation else "ok"],
              ["Ecken mit 90 Grad", f"{de(before_ang, 1)} → {de(after_ang, 1)} %",
               "flag" if stairs else "ok"],
              ["Geometrie gültig", f"ja, {reverted} Ring(e) ungeglättet" if new.is_valid else "nein",
               "ok" if new.is_valid else "flag"]]
    return {"path": out_path, "flag": dev > max_deviation or not new.is_valid or stairs, "gauges": gauges,
            "before": f"Treppenkanten, {vertex_count(geom)} Ecken",
            "after": f"geglättet, {vertex_count(new)} Ecken"}


def folder_attributes(folder_name, pattern):
    attrs = {"ordner": folder_name}
    match = re.match(pattern, folder_name) if pattern else None
    if match:
        attrs.update({k: (v or "") for k, v in match.groupdict().items()})
    return attrs, bool(match)


def merge(work, out_dir, folder_name, stems, pattern):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    out_path = str(Path(out_dir) / f"{folder_name}.gpkg")
    attrs, matched = folder_attributes(folder_name, pattern)
    rows, labels, srs = [], set(), None
    for stem in stems:
        done = read_metrics(work, stem).get("smooth") or {}
        if not done.get("path"):
            continue
        geom, stem_srs = read_geometry(done["path"])
        labels.add(crs_label(stem_srs))
        srs = srs or stem_srs
        rows.append((geom, {**attrs, "raster": stem}))
    if not rows:
        raise RuntimeError("kein einziges geglättetes Polygon im Ordner")
    if Path(out_path).exists():
        Path(out_path).unlink()
    source = ogr.GetDriverByName("GPKG").CreateDataSource(out_path)
    layer = source.CreateLayer(folder_name, srs, ogr.wkbMultiPolygon)
    fields = ["raster", *attrs]
    for key in fields:
        layer.CreateField(ogr.FieldDefn(key, ogr.OFTString))
    for geom, props in rows:
        feature = ogr.Feature(layer.GetLayerDefn())
        for key, value in props.items():
            feature.SetField(key, value)
        feature.SetGeometry(ogr.CreateGeometryFromWkb(geom.wkb))
        layer.CreateFeature(feature)
        feature = None
    written = layer.GetFeatureCount()
    layer, source = None, None
    gauges = [["Features", f"{written} von {len(stems)} Rastern",
               "ok" if written == len(stems) else "flag"],
              ["CRS im Ordner", sorted(labels)[0] if len(labels) == 1 else f"{len(labels)} verschiedene",
               "ok" if len(labels) == 1 else "flag"],
              ["Attribute aus dem Ordnernamen",
               ", ".join(f"{k}={v}" for k, v in attrs.items() if k != "ordner") if matched else "Muster passt nicht",
               "ok" if matched else "flag"]]
    return {"path": out_path, "flag": written != len(stems) or len(labels) != 1 or not matched,
            "gauges": gauges,
            "before": f"{len(stems)} Einzeldateien",
            "after": f"1 Layer, {written} Features"}

"""Visual and structural verification of a compressed PDF against its original.

Every page of both files is rendered at 150 dpi in the same colour space
(RGB, plus CMYK for print files) and compared with SSIM and CIEDE2000.
Structure (page boxes, image placements, fonts, annotations, layers,
overprint, spot colours...) is compared separately.
"""

import hashlib
import math

import numpy as np
import pikepdf
import pymupdf
from pikepdf import Array, Dictionary, Name, Stream
from scipy.ndimage import gaussian_filter

DPI = 150
TILE = 16                 # 16 px at 150 dpi ≈ 2.7 mm: the smallest "region"
BAND = 512                # rows processed at a time, bounds memory on big pages
PAD = 8                   # gaussian context above/below each band

SSIM_PAGE_MIN = 0.995
SSIM_TILE_MIN = 0.95
DE_MEAN_MAX = 1.0
DE_TILE_MAX = 3.0

STRICT = {"ssim_page_min": SSIM_PAGE_MIN, "ssim_region_min": SSIM_TILE_MIN,
          "de_mean_max": DE_MEAN_MAX, "de_region_max": DE_TILE_MAX}
# Screen preset: "no difference you'd notice at normal viewing size".
SCREEN = {"ssim_page_min": 0.985, "ssim_region_min": 0.70, "de_mean_max": 1.5, "de_region_max": 6.0}

pymupdf.TOOLS.mupdf_display_errors(False)


# ------------------------------------------------------------ colour math

def _srgb_to_lab(rgb):
    c = rgb.astype(np.float32) / 255.0
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    m = np.array([[0.4124564, 0.3575761, 0.1804375],
                  [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]], dtype=np.float32)
    xyz = lin @ m.T / np.array([0.95047, 1.0, 1.08883], dtype=np.float32)
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[:, 1] - 16, 500 * (f[:, 0] - f[:, 1]), 200 * (f[:, 1] - f[:, 2])], 1)


def delta_e2000(lab1, lab2):
    L1, a1, b1 = lab1.T
    L2, a2, b2 = lab2.T
    C1, C2 = np.hypot(a1, b1), np.hypot(a2, b2)
    Cb = (C1 + C2) / 2
    G = 0.5 * (1 - np.sqrt(Cb ** 7 / (Cb ** 7 + 25.0 ** 7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = np.hypot(a1p, b1), np.hypot(a2p, b2)
    h1p = np.degrees(np.arctan2(b1, a1p)) % 360
    h2p = np.degrees(np.arctan2(b2, a2p)) % 360
    dLp, dCp = L2 - L1, C2p - C1p
    dh = h2p - h1p
    dh = np.where(dh > 180, dh - 360, np.where(dh < -180, dh + 360, dh))
    dh = np.where(C1p * C2p == 0, 0, dh)
    dHp = 2 * np.sqrt(C1p * C2p) * np.sin(np.radians(dh) / 2)
    Lbp, Cbp = (L1 + L2) / 2, (C1p + C2p) / 2
    hs = h1p + h2p
    hbp = np.where(np.abs(h1p - h2p) > 180, np.where(hs < 360, hs + 360, hs - 360), hs) / 2
    hbp = np.where(C1p * C2p == 0, hs, hbp)
    T = (1 - 0.17 * np.cos(np.radians(hbp - 30)) + 0.24 * np.cos(np.radians(2 * hbp))
         + 0.32 * np.cos(np.radians(3 * hbp + 6)) - 0.20 * np.cos(np.radians(4 * hbp - 63)))
    dtheta = 30 * np.exp(-(((hbp - 275) / 25) ** 2))
    Rc = 2 * np.sqrt(Cbp ** 7 / (Cbp ** 7 + 25.0 ** 7))
    Sl = 1 + (0.015 * (Lbp - 50) ** 2) / np.sqrt(20 + (Lbp - 50) ** 2)
    Sc = 1 + 0.045 * Cbp
    Sh = 1 + 0.015 * Cbp * T
    Rt = -np.sin(np.radians(2 * dtheta)) * Rc
    return np.sqrt((dLp / Sl) ** 2 + (dCp / Sc) ** 2 + (dHp / Sh) ** 2
                   + Rt * (dCp / Sc) * (dHp / Sh))


# ------------------------------------------------------------ pixel metrics

def _ssim_map(x, y):
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    g = lambda z: gaussian_filter(z, 1.5, truncate=3.5)
    mx, my = g(x), g(y)
    sxx = g(x * x) - mx * mx
    syy = g(y * y) - my * my
    sxy = g(x * y) - mx * my
    return ((2 * mx * my + C1) * (2 * sxy + C2)) / ((mx * mx + my * my + C1) * (sxx + syy + C2))


def _tile_means(m, tile=TILE):
    h, w = m.shape
    th, tw = math.ceil(h / tile), math.ceil(w / tile)
    p = np.pad(m, ((0, th * tile - h), (0, tw * tile - w)), mode="edge")
    return p.reshape(th, tile, tw, tile).mean(axis=(1, 3))


def compare(a, b, with_de=True, t=STRICT):
    """Compare two renders (H, W, C) uint8. Returns metrics plus a boolean
    tile grid of failing regions."""
    if a.shape != b.shape:
        return {"ssim": 0.0, "ssim_tile_min": 0.0, "de_mean": 99.0, "de_tile_max": 99.0,
                "identical": False, "bad_tiles": None, "size_mismatch": True}
    if np.array_equal(a, b):
        return {"ssim": 1.0, "ssim_tile_min": 1.0, "de_mean": 0.0, "de_tile_max": 0.0,
                "identical": True, "bad_tiles": None}
    h, w, c = a.shape
    ssim_sum, de_sum = 0.0, 0.0
    ssim_tiles, de_tiles = [], []
    band = BAND - BAND % TILE
    for y0 in range(0, h, band):
        y1 = min(h, y0 + band)
        p0, p1 = max(0, y0 - PAD), min(h, y1 + PAD)
        sa, sb = a[p0:p1], b[p0:p1]
        if np.array_equal(sa, sb):
            rows = y1 - y0
            ssim_sum += rows * w
            ssim_tiles.append(np.ones((math.ceil(rows / TILE), math.ceil(w / TILE)), np.float32))
            de_tiles.append(np.zeros_like(ssim_tiles[-1]))
            continue
        fa, fb = sa.astype(np.float32), sb.astype(np.float32)
        smap = np.mean([_ssim_map(fa[..., k], fb[..., k]) for k in range(c)], axis=0)
        smap = smap[y0 - p0: y0 - p0 + (y1 - y0)]
        ssim_sum += float(smap.sum(dtype=np.float64))
        ssim_tiles.append(_tile_means(smap))
        if with_de:
            ba, bb = a[y0:y1].reshape(-1, c), b[y0:y1].reshape(-1, c)
            diff = np.any(ba != bb, axis=1)
            de = np.zeros(ba.shape[0], np.float32)
            if diff.any():
                de[diff] = delta_e2000(_srgb_to_lab(ba[diff]), _srgb_to_lab(bb[diff]))
            de_sum += float(de.sum(dtype=np.float64))
            de_tiles.append(_tile_means(de.reshape(y1 - y0, w)))
        else:
            de_tiles.append(np.zeros_like(ssim_tiles[-1]))
    st = np.vstack(ssim_tiles)
    dt = np.vstack(de_tiles)
    bad = (st < t["ssim_region_min"]) | (dt > t["de_region_max"])
    return {
        "ssim": ssim_sum / (h * w),
        "ssim_tile_min": float(st.min()),
        "de_mean": de_sum / (h * w) if with_de else 0.0,
        "de_tile_max": float(dt.max()) if with_de else 0.0,
        "identical": False,
        "bad_tiles": bad if bad.any() else None,
    }


def render(page, cs, dpi=DPI):
    pix = page.get_pixmap(dpi=dpi, colorspace=cs, alpha=False, annots=True)
    return np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width, pix.n)


# ------------------------------------------------------------ structure

def _reachable(pdf):
    """Yield every object reachable from the trailer once (direct and indirect)."""
    seen = set()
    stack = [pdf.trailer]
    while stack:
        o = stack.pop()
        if isinstance(o, pikepdf.Object) and o.is_indirect:
            if o.objgen in seen:
                continue
            seen.add(o.objgen)
        if isinstance(o, Stream):
            yield o
            stack.extend(v for _, v in o.stream_dict.items() if isinstance(v, pikepdf.Object))
        elif isinstance(o, Dictionary):
            yield o
            stack.extend(v for _, v in o.items() if isinstance(v, pikepdf.Object))
        elif isinstance(o, Array):
            yield o
            stack.extend(v for v in o if isinstance(v, pikepdf.Object))


def _sig(v):
    """Stable text form of a value: indirect objects are described by
    content, never by object number (which changes on every rewrite)."""
    if v is None:
        return "-"
    if isinstance(v, Name):
        return str(v)
    if isinstance(v, (bool, int, float)):
        return str(v)
    if isinstance(v, Array):
        return "[" + " ".join(str(x) if isinstance(x, Name) else type(x).__name__ for x in v) + "]"
    if isinstance(v, Stream):
        return "stream"
    if isinstance(v, Dictionary):
        return "dict"
    return str(v)


def _num(v):
    try:
        return round(float(v), 3)
    except Exception:
        return str(v)


def doc_structure(path):
    """Document-level properties that must survive compression unchanged."""
    s = {"spot_colors": set(), "overprint": set(), "blend_modes": set(),
         "transparency_groups": set(), "output_intents": [], "uses_cmyk": False}
    with pikepdf.open(path) as pdf:
        for o in _reachable(pdf):
            if isinstance(o, Array) and len(o) >= 2 and isinstance(o[0], pikepdf.Object):
                fam = str(o[0]) if isinstance(o[0], Name) else ""
                if fam == "/Separation":
                    s["spot_colors"].add(str(o[1]))
                    s["uses_cmyk"] = True
                elif fam == "/DeviceN" and isinstance(o[1], Array):
                    s["spot_colors"].update(str(x) for x in o[1])
                    s["uses_cmyk"] = True
                elif fam == "/ICCBased" and isinstance(o[1], Stream) and int(o[1].get("/N", 0)) == 4:
                    s["uses_cmyk"] = True
            if isinstance(o, (Dictionary, Stream)):
                if o.get("/ColorSpace") == Name.DeviceCMYK:
                    s["uses_cmyk"] = True
                if o.get("/Type") == Name.ExtGState or any(k in o for k in ("/OP", "/op", "/OPM")):
                    if any(k in o for k in ("/OP", "/op", "/OPM")):
                        s["overprint"].add(tuple(_sig(o.get(k)) for k in ("/OP", "/op", "/OPM")))
                    if "/BM" in o:
                        s["blend_modes"].add(_sig(o.get("/BM")))
                g = o.get("/Group")
                if isinstance(g, Dictionary) and g.get("/S") == Name.Transparency:
                    s["transparency_groups"].add(tuple(_sig(g.get(k)) for k in ("/CS", "/I", "/K")))
        for oi in pdf.Root.get("/OutputIntents", Array()):
            prof = oi.get("/DestOutputProfile")
            h = hashlib.sha256(prof.read_bytes()).hexdigest()[:16] if isinstance(prof, Stream) else None
            s["output_intents"].append((_sig(oi.get("/S")), str(oi.get("/OutputConditionIdentifier")), h))
            if isinstance(prof, Stream) and int(prof.get("/N", 0)) == 4:
                s["uses_cmyk"] = True
        boxes = []
        for p in pdf.pages:
            boxes.append({k: [_num(v) for v in getattr(p, k)]
                          for k in ("mediabox", "cropbox", "bleedbox", "trimbox", "artbox")})
        s["page_boxes"] = boxes
    for k in ("spot_colors", "overprint", "blend_modes", "transparency_groups"):
        s[k] = sorted(s[k])
    return s


def page_structure(doc, i, boxes):
    page = doc[i]
    imgs = sorted((round(b[0], 1), round(b[1], 1), round(b[2], 1), round(b[3], 1))
                  for b in (info["bbox"] for info in page.get_image_info()))
    fonts = sorted({(f[3], f[2], f[1]) for f in page.get_fonts(full=True)})
    annots = sorted((a.type[1], tuple(round(v, 1) for v in a.rect)) for a in page.annots())
    links = sorted((l.get("kind"), tuple(round(v, 1) for v in l["from"]),
                    l.get("uri") or l.get("page")) for l in page.get_links())
    return {"boxes": boxes[i], "rotate": page.rotation, "images": imgs, "fonts": fonts,
            "annots": annots, "links": links}


def _doc_level(doc):
    return {
        "page_count": doc.page_count,
        "outline": [tuple(t[:3]) for t in doc.get_toc(simple=True)],
        "layers": sorted((v.get("name"), v.get("on"), str(v.get("intent")), str(v.get("usage")))
                         for v in doc.get_ocgs().values()),
        "form_fields": int(doc.is_form_pdf or 0),
    }


_DOC_LABELS = {
    "page_count": "Page count", "outline": "Bookmarks", "layers": "Optional content layers",
    "form_fields": "Form fields", "spot_colors": "Spot colours", "overprint": "Overprint settings",
    "blend_modes": "Blend modes", "transparency_groups": "Transparency groups",
    "output_intents": "OutputIntent / ICC",
}
_PAGE_LABELS = {"boxes": "Page boxes", "rotate": "Rotation", "images": "Image placements",
                "fonts": "Fonts", "annots": "Annotations", "links": "Links"}


# ------------------------------------------------------------ driver

def verify(orig_path, new_path, progress=None, dpi=DPI, t=STRICT, only=None):
    """Compare every page (or just the 1-based page numbers in `only`) at `dpi`.
    Returns a report dict; report['passed'] is the gate."""
    so, sn = doc_structure(orig_path), doc_structure(new_path)
    do, dn = pymupdf.open(orig_path), pymupdf.open(new_path)
    try:
        lo, ln = _doc_level(do), _doc_level(dn)
        doc_checks = []
        for k, label in _DOC_LABELS.items():
            a = lo.get(k, so.get(k))
            b = ln.get(k, sn.get(k))
            doc_checks.append({"check": label, "ok": a == b})
        doc_ok = all(c["ok"] for c in doc_checks)
        use_cmyk = so["uses_cmyk"]

        pages = []
        n = min(do.page_count, dn.page_count)
        todo = [i for i in range(n) if only is None or i + 1 in only]
        for k, i in enumerate(todo):
            if progress:
                progress(k, len(todo))
            po = page_structure(do, i, so["page_boxes"])
            pn = page_structure(dn, i, sn["page_boxes"])
            struct_fail = [_PAGE_LABELS[k] for k in _PAGE_LABELS if po[k] != pn[k]]

            rgb = compare(render(do[i], pymupdf.csRGB, dpi), render(dn[i], pymupdf.csRGB, dpi), t=t)
            cmyk = None
            if use_cmyk:
                cmyk = compare(render(do[i], pymupdf.csCMYK, dpi), render(dn[i], pymupdf.csCMYK, dpi),
                               with_de=False, t=t)
            visual_fail = []
            if rgb.get("size_mismatch"):
                visual_fail.append("render size differs")
            sp, sr, dm, dr = t["ssim_page_min"], t["ssim_region_min"], t["de_mean_max"], t["de_region_max"]
            if rgb["ssim"] < sp:
                visual_fail.append(f"SSIM {rgb['ssim']:.4f} < {sp}")
            if rgb["ssim_tile_min"] < sr:
                visual_fail.append(f"local SSIM {rgb['ssim_tile_min']:.3f} < {sr}")
            if rgb["de_mean"] >= dm:
                visual_fail.append(f"mean ΔE {rgb['de_mean']:.2f} ≥ {dm}")
            if rgb["de_tile_max"] > dr:
                visual_fail.append(f"region ΔE {rgb['de_tile_max']:.2f} > {dr}")
            if cmyk:
                if cmyk["ssim"] < sp:
                    visual_fail.append(f"CMYK SSIM {cmyk['ssim']:.4f} < {sp}")
                if cmyk["ssim_tile_min"] < sr:
                    visual_fail.append(f"CMYK local SSIM {cmyk['ssim_tile_min']:.3f} < {sr}")

            bad = rgb.get("bad_tiles")
            if cmyk is not None and cmyk.get("bad_tiles") is not None:
                bad = cmyk["bad_tiles"] if bad is None else (bad | cmyk["bad_tiles"])
            pages.append({
                "page": i + 1,
                "passed": not struct_fail and not visual_fail,
                "identical": rgb["identical"] and (cmyk is None or cmyk["identical"]),
                "ssim": round(rgb["ssim"], 5),
                "ssim_tile_min": round(rgb["ssim_tile_min"], 4),
                "de_mean": round(rgb["de_mean"], 3),
                "de_tile_max": round(rgb["de_tile_max"], 2),
                "cmyk_ssim": None if cmyk is None else round(cmyk["ssim"], 5),
                "structure_failures": struct_fail,
                "visual_failures": visual_fail,
                "_bad_tiles": bad,
            })
        if progress:
            progress(n, n)
    finally:
        do.close()
        dn.close()
    return {
        "passed": doc_ok and all(p["passed"] for p in pages) and lo["page_count"] == ln["page_count"],
        "doc_checks": doc_checks,
        "pages": pages,
        "cmyk_checked": use_cmyk,
        "thresholds": dict(t, dpi=dpi, region_px=TILE, mode="screen" if t is SCREEN else "strict"),
    }

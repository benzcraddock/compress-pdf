"""Per-image optimization.

Only images drawn directly by a page's own content stream are candidates.
Image data is replaced in place on the same object, so every reference,
transformation matrix and Form /BBox stays exactly as it was. The colour
space entry is never touched, which keeps CMYK, ICC, spot and indexed
colour identical.
"""

import io
import math
import zlib
from dataclasses import asdict, dataclass, field

import numpy as np
import pikepdf
from PIL import Image
from pikepdf import Array, Dictionary, Name, Stream

Image.MAX_IMAGE_PIXELS = None

_LOSSLESS_FILTERS = {"/FlateDecode", "/Fl", "/LZWDecode", "/LZW", "/ASCII85Decode", "/A85",
                     "/ASCIIHexDecode", "/AHx", "/RunLengthDecode", "/RL"}
_DEVICE_CS = {"/DeviceGray": 1, "/G": 1, "/DeviceRGB": 3, "/RGB": 3, "/DeviceCMYK": 4, "/CMYK": 4}
_MIN_PIXELS = 64 * 64
_MIN_BYTES = 8 * 1024


@dataclass
class ImageResult:
    objgen: tuple
    pages: list
    width: int
    height: int
    colorspace: str
    filter: str
    bytes_before: int
    ppi: float | None = None
    action: str = "skipped"          # recompressed | skipped | reverted
    reason: str = ""
    new_width: int | None = None
    new_height: int | None = None
    new_filter: str | None = None
    bytes_after: int | None = None
    kind: str | None = None           # photo | graphic
    score: float | None = None        # image-space SSIM vs. original, for blame ranking
    mask_bytes_before: int | None = None
    mask_bytes_after: int | None = None

    def to_dict(self):
        d = asdict(self)
        d["objgen"] = list(self.objgen)
        d["id"] = f"{self.objgen[0]} {self.objgen[1]} R"
        return d


# ------------------------------------------------------------ inspection

def _filters(img):
    f = img.get("/Filter")
    if f is None:
        return []
    if isinstance(f, Array):
        return [str(x) for x in f]
    return [str(f)]


def colorspace_info(img):
    """Returns (components, label) when the colour space is one we can
    re-encode without converting, else (None, label)."""
    cs = img.get("/ColorSpace")
    if cs is None:
        return None, "none"
    if isinstance(cs, Name):
        n = _DEVICE_CS.get(str(cs))
        return n, str(cs)[1:]
    if isinstance(cs, Array) and len(cs):
        family = str(cs[0])
        if family == "/ICCBased":
            try:
                n = int(cs[1].get("/N"))
            except Exception:
                return None, "ICCBased"
            return (n if n in (1, 3, 4) else None), f"ICCBased/{n}"
        if family in ("/CalRGB", "/CalGray"):
            return (3 if family == "/CalRGB" else 1), family[1:]
        return None, family[1:]
    return None, "unknown"


def _resources(page_obj):
    node = page_obj
    for _ in range(64):
        if node is None:
            break
        r = node.get("/Resources")
        if r is not None:
            return r
        node = node.get("/Parent")
    return Dictionary()


def _is_image(o):
    return isinstance(o, Stream) and o.get("/Subtype") == Name.Image


def _mat_mul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return (a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F)


def collect(pdf):
    """Find every image, where it is used, and its top-level placements.

    Returns (images, contexts, placements):
      images:     objgen -> Stream
      contexts:   objgen -> set of {'page','form','pattern','type3','mask','alternate'}
      placements: objgen -> list of (page_index, ctm)
    """
    images, contexts, placements = {}, {}, {}
    unparseable = set()

    def note(img, ctx):
        images[img.objgen] = img
        contexts.setdefault(img.objgen, set()).add(ctx)

    def scan_resources(res, ctx):
        xo = res.get("/XObject") if res is not None else None
        if isinstance(xo, Dictionary):
            for _, x in xo.items():
                if _is_image(x):
                    note(x, ctx)

    # Images reachable from anything other than a page's own resources.
    for obj in pdf.objects:
        if isinstance(obj, Stream) and obj.get("/Subtype") == Name.Form:
            scan_resources(obj.get("/Resources"), "form")
        elif isinstance(obj, (Dictionary, Stream)) and "/PatternType" in obj:
            scan_resources(obj.get("/Resources"), "pattern")
        elif isinstance(obj, Dictionary) and obj.get("/Subtype") == Name.Type3:
            scan_resources(obj.get("/Resources"), "type3")
        if _is_image(obj):
            for key in ("/SMask", "/Mask"):
                m = obj.get(key)
                if _is_image(m):
                    note(m, "mask")
            alts = obj.get("/Alternates")
            if isinstance(alts, Array):
                for a in alts:
                    im = a.get("/Image") if isinstance(a, Dictionary) else None
                    if _is_image(im):
                        note(im, "alternate")
        if isinstance(obj, Dictionary) and obj.get("/Type") == Name.ExtGState:
            sm = obj.get("/SMask")
            if isinstance(sm, Dictionary) and isinstance(sm.get("/G"), Stream):
                scan_resources(sm.G.get("/Resources"), "form")

    for pi, page in enumerate(pdf.pages):
        res = _resources(page.obj)
        scan_resources(res, "page")
        xo = res.get("/XObject")
        if not isinstance(xo, Dictionary):
            continue
        uu = float(page.obj.get("/UserUnit", 1))
        try:
            ops = pikepdf.parse_content_stream(page)
        except Exception:
            for _, x in xo.items():
                if _is_image(x):
                    unparseable.add(x.objgen)
            continue
        ctm, stack = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0), []
        for instr in ops:
            op = str(instr.operator)
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                ctm = stack.pop() if stack else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            elif op == "cm" and len(instr.operands) == 6:
                ctm = _mat_mul(tuple(float(v) for v in instr.operands), ctm)
            elif op == "Do" and instr.operands:
                x = xo.get(str(instr.operands[0]))
                if _is_image(x):
                    scaled = tuple(v * uu for v in ctm[:4]) + ctm[4:]
                    placements.setdefault(x.objgen, []).append((pi, scaled))

    for og in unparseable:
        contexts.setdefault(og, set()).add("unparseable")
    return images, contexts, placements


def effective_ppi(img, placement_list):
    """Lowest resolution at which the image is shown (its largest placement)."""
    w, h = int(img.Width), int(img.Height)
    best = math.inf
    for _, (a, b, c, d, _e, _f) in placement_list:
        wpt, hpt = math.hypot(a, b), math.hypot(c, d)
        if wpt < 1e-6 or hpt < 1e-6:
            return None
        best = min(best, w / (wpt / 72.0), h / (hpt / 72.0))
    return None if best is math.inf else best


# ------------------------------------------------------------ pixels

def _decode(img, n):
    """Raw 8-bit samples as (H, W, n) uint8, in the PDF's own sample space."""
    w, h = int(img.Width), int(img.Height)
    flt = _filters(img)
    if flt and flt[-1] in ("/DCTDecode", "/DCT"):
        if len(flt) != 1:
            return None
        pil = Image.open(io.BytesIO(img.read_raw_bytes()))
        pil.load()
        want = {1: "L", 3: "RGB", 4: "CMYK"}[n]
        if pil.mode != want or pil.size != (w, h):
            return None
        arr = np.asarray(pil, dtype=np.uint8).reshape(h, w, n)
        if n == 4:
            # Pillow reads CMYK JPEGs as Adobe-inverted; undo it so we hold
            # the bytes that are actually stored in the file.
            arr = 255 - arr
        return arr
    data = img.read_bytes()
    need = w * h * n
    if len(data) < need:
        return None
    return np.frombuffer(data[:need], dtype=np.uint8).reshape(h, w, n)


def classify(arr):
    """'photo' or 'graphic' from colour count and edge content. Hard edges
    (text, line art) always stay lossless; many distinct colours, or a fine
    grain with no flat areas, mean continuous tone even on a flat background."""
    h, w, n = arr.shape
    step = max(1, int(math.ceil(h * w / 4_000_000)))
    s = arr[::step]                                   # keep horizontal adjacency intact
    flat = s.reshape(-1, n).astype(np.uint32)

    def count(shift):
        packed = np.zeros(flat.shape[0], dtype=np.uint64)
        for i in range(n):
            packed = (packed << np.uint64(8 - shift)) | (flat[:, i] >> shift).astype(np.uint64)
        return np.unique(packed, return_counts=True)[1]

    counts = count(2)
    exact = count(0).size
    g = s.astype(np.int16).mean(axis=2) if n > 1 else s[..., 0].astype(np.int16)
    dx = np.abs(np.diff(g, axis=1))
    flat_ratio = float((dx < 1).mean())
    sharp = float((dx > 96).mean())
    if sharp > 0.03:
        return "graphic", f"hard edges ({sharp:.1%})"
    if exact >= 8192:
        return "photo", f"continuous tone ({exact} colours)"
    if flat_ratio < 0.2 and counts.size < 512:
        return "photo", f"fine grain / texture ({flat_ratio:.0%} flat)"
    if counts.size < 512:
        return "graphic", f"{counts.size} colours"
    top = np.sort(counts)[::-1][:16].sum() / counts.sum()
    if top > 0.5:
        return "graphic", f"flat colour ({top:.0%} in 16 colours)"
    if flat_ratio > 0.55:
        return "graphic", f"flat areas ({flat_ratio:.0%})"
    return "photo", "continuous tone"


def _resize(arr, nw, nh, kind):
    n = arr.shape[2]
    mode = {1: "L", 3: "RGB", 4: "CMYK"}[n]
    pil = Image.fromarray(arr.reshape(arr.shape[0], arr.shape[1]) if n == 1 else arr, mode)
    method = Image.Resampling.LANCZOS if kind == "photo" else Image.Resampling.BOX
    out = pil.resize((nw, nh), method)
    return np.asarray(out, dtype=np.uint8).reshape(nh, nw, n)


def encode_jpeg(arr, quality, subsampling):
    h, w, n = arr.shape
    if n == 4:
        pil = Image.fromarray(255 - arr, "CMYK")     # Pillow re-inverts on save
    elif n == 3:
        pil = Image.fromarray(arr, "RGB")
    else:
        pil = Image.fromarray(arr.reshape(h, w), "L")
    buf = io.BytesIO()
    kw = {"quality": quality, "optimize": True, "progressive": False}
    if n == 3:
        kw["subsampling"] = subsampling
    pil.save(buf, "JPEG", **kw)
    return buf.getvalue()


def encode_flate(arr):
    """Flate with per-row PNG predictors (None/Sub/Up), PDF /Predictor 15."""
    h, w, n = arr.shape
    a = arr.reshape(h, w * n).astype(np.int16)
    sub = a.copy()
    sub[:, n:] -= a[:, :-n]
    up = a.copy()
    up[1:] -= a[:-1]
    cands = np.stack([a, sub, up]) & 0xFF                 # (3, h, w*n)
    cost = np.minimum(cands, 256 - cands).sum(axis=2)    # (3, h)
    choice = cost.argmin(axis=0)
    rows = cands[choice, np.arange(h)].astype(np.uint8)
    out = np.empty((h, w * n + 1), dtype=np.uint8)
    out[:, 0] = choice                                   # PNG filter types 0,1,2
    out[:, 1:] = rows
    return zlib.compress(out.tobytes(), 9)


# ------------------------------------------------------------ driver

def _human(n):
    return f"{n / 1e6:.1f} MB" if n >= 1e6 else f"{n / 1e3:.0f} KB"


def _score(orig, new):
    """SSIM between original and re-encoded pixels, compared at a common
    small size. Only used to rank which change most likely broke a page."""
    from .verify import _ssim_map
    h, w = new.shape[:2]
    k = min(1.0, 512 / max(h, w))
    size = (max(8, round(w * k)), max(8, round(h * k)))
    vals = []
    for c in range(orig.shape[2]):
        a = np.asarray(Image.fromarray(np.ascontiguousarray(orig[..., c])).resize(size, Image.Resampling.BOX), np.float32)
        b = np.asarray(Image.fromarray(np.ascontiguousarray(new[..., c])).resize(size, Image.Resampling.BOX), np.float32)
        vals.append(float(_ssim_map(a, b).mean()))
    return round(min(vals), 5)


def _decode_jpeg_bytes(data, n):
    pil = Image.open(io.BytesIO(data))
    arr = np.asarray(pil, dtype=np.uint8)
    arr = arr.reshape(arr.shape[0], arr.shape[1], n)
    return 255 - arr if n == 4 else arr


def _all_placements(src_path):
    """Every placement of every image, including those drawn from inside
    Form XObjects, as MuPDF sees them: xref -> [(page_index, matrix)]."""
    import pymupdf
    out = {}
    with pymupdf.open(src_path) as doc:
        for pi, page in enumerate(doc):
            for info in page.get_image_info(xrefs=True):
                if info.get("xref"):
                    out.setdefault(info["xref"], []).append((pi, tuple(info["transform"])))
    return out


def optimize_images(pdf, opts, exclude=frozenset(), progress=None, softer=None, softened=frozenset(),
                    src_path=None, merged=None):
    """Recompress/downsample eligible images in place. Returns ImageResults.
    `exclude`: images to leave untouched; `softened`: images to process with
    the gentler `softer` settings because the preset's own failed verification."""
    base_opts = opts
    images, contexts, placements = collect(pdf)
    allow_forms = bool(opts.get("forms"))
    allow_masked = bool(opts.get("masked"))
    resize_masks = bool(opts.get("resize_masks"))
    every = _all_placements(src_path) if allow_forms and src_path else {}
    # A merged duplicate's placements now belong to the object that survived.
    for dup, canon in (merged or {}).items():
        if dup[0] in every:
            every.setdefault(canon[0], []).extend(every[dup[0]])
    mask_parents = {}
    for o in images.values():
        m = o.get("/SMask")
        if _is_image(m):
            mask_parents[m.objgen] = mask_parents.get(m.objgen, 0) + 1
    results = []
    order = sorted(images)
    for i, og in enumerate(order):
        if progress:
            progress(i, len(order))
        img = images[og]
        opts = softer if (og in softened and softer) else base_opts
        ctx = contexts.get(og, set())
        pls = placements.get(og, [])
        if allow_forms and ctx <= {"page", "form"}:
            pls = every.get(og[0], pls)
        n, cs_label = colorspace_info(img)
        flt = _filters(img)
        try:
            raw_len = len(img.read_raw_bytes())
        except Exception:
            raw_len = 0
        r = ImageResult(
            objgen=og, pages=sorted({p + 1 for p, _ in pls}),
            width=int(img.get("/Width", 0)), height=int(img.get("/Height", 0)),
            colorspace=cs_label, filter="+".join(f[1:] for f in flt) or "none",
            bytes_before=raw_len)
        results.append(r)

        def skip(reason):
            r.reason = reason

        if "mask" in ctx and ctx == {"mask"}:
            r.action = "mask"           # reported alongside its parent image only
            continue
        if not opts["images"]:
            skip("Lossless preset: images are never recompressed")
            continue
        if og in exclude:
            r.action = "reverted"
            skip("Reverted: re-encoding failed visual verification")
            continue
        if ctx - ({"page", "form"} if allow_forms else {"page"}):
            where = ", ".join(sorted(ctx - {"page"}))
            skip(f"Used inside {where}: left untouched")
            continue
        if not pls:
            skip("Not drawn directly by page content")
            continue
        if img.get("/ImageMask", False):
            skip("Stencil mask")
            continue
        # With `masked`, a soft-masked image's colour data may be re-encoded at
        # its original size; the SMask object itself is never modified.
        smask_ok = allow_masked and _is_image(img.get("/SMask"))
        if ("/SMask" in img and not smask_ok) or "/Mask" in img or int(img.get("/SMaskInData", 0)):
            skip("Has transparency mask (SMask/Mask): left untouched")
            continue
        if n is None:
            skip(f"{cs_label} colour space: kept exactly as-is")
            continue
        if int(img.get("/BitsPerComponent", 0)) != 8:
            skip(f"{img.get('/BitsPerComponent')}-bit samples")
            continue
        is_dct = bool(flt) and flt[-1] in ("/DCTDecode", "/DCT")
        if not is_dct and any(f not in _LOSSLESS_FILTERS for f in flt):
            skip(f"{r.filter} encoding: left untouched")
            continue
        if r.width * r.height < _MIN_PIXELS or raw_len < _MIN_BYTES:
            skip("Too small to matter")
            continue

        ppi = effective_ppi(img, pls)
        r.ppi = None if ppi is None else round(ppi, 1)
        if ppi is None:
            skip("Degenerate placement matrix")
            continue
        # Ignore rounding-level overshoot: a <5% shrink isn't worth a re-encode.
        downsample = ppi > opts["threshold"] and opts["target"] / ppi < 0.95
        mask = img.get("/SMask") if smask_ok else None
        if smask_ok and downsample:
            # The mask may only be resized together with its image, to the same
            # new pixel grid, and only when nothing else shares it.
            mflt = _filters(mask)
            if not (resize_masks and mask_parents.get(mask.objgen) == 1
                    and int(mask.get("/BitsPerComponent", 0)) == 8
                    and (int(mask.Width), int(mask.Height)) == (r.width, r.height)
                    and "/Decode" not in mask
                    and all(f in _LOSSLESS_FILTERS for f in mflt)):
                downsample = False
        if is_dct and not downsample:
            skip(f"Already JPEG at {ppi:.0f} ppi (≤ {opts['threshold']}): no re-encode")
            continue

        try:
            arr = _decode(img, n)
        except Exception as e:
            arr = None
            skip(f"Could not decode ({e.__class__.__name__})")
            continue
        if arr is None:
            skip("Could not decode samples faithfully")
            continue

        if is_dct:
            kind, why = "photo", "already JPEG"
        else:
            kind, why = classify(arr)
        r.kind = kind

        orig_arr = arr
        nw, nh = r.width, r.height
        if downsample:
            f = opts["target"] / ppi
            nw, nh = max(1, round(r.width * f)), max(1, round(r.height * f))
            arr = _resize(arr, nw, nh, kind)

        if kind == "photo":
            data = encode_jpeg(arr, opts["quality"], opts["subsampling"])
            new_filter, parms = Name.DCTDecode, None
        else:
            data = encode_flate(arr)
            new_filter = Name.FlateDecode
            parms = Dictionary(Predictor=15, Colors=n, BitsPerComponent=8, Columns=nw)

        mask_data = None
        old_mask_len = 0
        if mask is not None and downsample:
            m = _decode(mask, 1)
            if m is None:
                skip("Could not decode its soft mask")
                continue
            mask_data = encode_flate(_resize(m, nw, nh, "graphic"))
            old_mask_len = len(mask.read_raw_bytes())

        if len(data) + len(mask_data or b"") > 0.9 * (raw_len + old_mask_len):
            skip(f"Re-encode not ≥10% smaller ({kind}, {why})")
            continue

        new_px = _decode_jpeg_bytes(data, n) if kind == "photo" else arr
        if new_px.shape[:2] != orig_arr.shape[:2]:
            ref = _resize(orig_arr, nw, nh, kind)    # judge encoding loss at the new size
        else:
            ref = orig_arr
        try:
            r.score = _score(ref, new_px)
        except Exception:
            r.score = None

        if "/DecodeParms" in img:
            del img.DecodeParms
        img.write(data, filter=new_filter, decode_parms=parms)
        if (nw, nh) != (r.width, r.height):
            img.Width, img.Height = nw, nh
        if mask_data is not None:
            if "/DecodeParms" in mask:
                del mask.DecodeParms
            mask.write(mask_data, filter=Name.FlateDecode,
                       decode_parms=Dictionary(Predictor=15, Colors=1, BitsPerComponent=8, Columns=nw))
            mask.Width, mask.Height = nw, nh
            r.mask_bytes_before, r.mask_bytes_after = old_mask_len, len(mask_data)
        # Kept for the per-page pre-check (not part of the report).
        r._new = {
            "xref": og[0], "data": data, "filter": str(new_filter), "w": nw, "h": nh,
            "parms": (f"<</Predictor 15/Colors {n}/BitsPerComponent 8/Columns {nw}>>"
                      if parms is not None else None),
            "mask": None if mask_data is None else {
                "xref": mask.objgen[0], "data": mask_data, "filter": "/FlateDecode", "w": nw, "h": nh,
                "parms": f"<</Predictor 15/Colors 1/BitsPerComponent 8/Columns {nw}>>"},
        }
        r.action = "recompressed"
        r.new_width, r.new_height = nw, nh
        r.new_filter = str(new_filter)[1:]
        r.bytes_after = len(data)
        bits = [f"{kind} → {'JPEG q' + str(opts['quality']) if kind == 'photo' else 'Flate'}"]
        if downsample:
            bits.append(f"{ppi:.0f} → {opts['target']} ppi")
        if mask_data is not None:
            bits.append(f"soft mask resized with it ({_human(old_mask_len)} → {_human(len(mask_data))})")
        elif mask is not None:
            bits.append("soft mask kept byte-identical")
        bits.append(why)
        if opts is not base_opts:
            bits.append("eased to Balanced settings after failing verification")
        r.reason = "; ".join(bits)
    if progress:
        progress(len(order), len(order))
    return [r for r in results if r.action != "mask"]

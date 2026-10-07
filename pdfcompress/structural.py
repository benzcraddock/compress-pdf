"""Lossless structural work and non-visual data stripping.

Nothing in here changes how a page renders: thumbnails are never drawn,
duplicate objects are byte-identical, and PieceInfo / XMP history are
private application data that viewers ignore.
"""

import hashlib
import re
import xml.etree.ElementTree as ET

import pikepdf
from pikepdf import Array, Dictionary, Name, Stream

# Dictionaries (not streams) that are safe to merge when identical.
_DEDUP_DICT_TYPES = {"/Font", "/FontDescriptor", "/ExtGState"}


def drop_thumbnails(pdf):
    n = 0
    for page in pdf.pages:
        if Name.Thumb in page.obj:
            del page.obj[Name.Thumb]
            n += 1
    return n


def _key(obj, top=True):
    """Canonical, hashable form of an object. Nested indirect objects are
    represented by their object number so the key stays shallow."""
    if not top and isinstance(obj, pikepdf.Object) and obj.is_indirect:
        return ("R", obj.objgen)
    if isinstance(obj, Stream):
        items = tuple(sorted((k, _key(v, False)) for k, v in obj.stream_dict.items()
                             if k != "/Length"))
        return ("S", items, hashlib.sha256(obj.read_raw_bytes()).digest())
    if isinstance(obj, Dictionary):
        return ("D", tuple(sorted((k, _key(v, False)) for k, v in obj.items())))
    if isinstance(obj, Array):
        return ("A", tuple(_key(v, False) for v in obj))
    return ("V", type(obj).__name__, repr(obj))


def _dedup_candidate(obj):
    if isinstance(obj, Stream):
        # Page content streams can be shared safely too, but keep the scope to
        # resources: images, fonts, ICC profiles, forms, functions.
        return True
    if isinstance(obj, Dictionary):
        return str(obj.get("/Type", "")) in _DEDUP_DICT_TYPES
    return False


def _rewrite_refs(container, remap):
    """Point every direct reference inside `container` at its canonical twin."""
    if isinstance(container, Stream):
        container = container.stream_dict
    if isinstance(container, Dictionary):
        for k, v in list(container.items()):
            if isinstance(v, pikepdf.Object) and v.is_indirect:
                if v.objgen in remap:
                    container[k] = remap[v.objgen]
            elif isinstance(v, (Dictionary, Array)):
                _rewrite_refs(v, remap)
    elif isinstance(container, Array):
        for i, v in enumerate(list(container)):
            if isinstance(v, pikepdf.Object) and v.is_indirect:
                if v.objgen in remap:
                    container[i] = remap[v.objgen]
            elif isinstance(v, (Dictionary, Array)):
                _rewrite_refs(v, remap)


def deduplicate(pdf, max_passes=4):
    """Merge byte-identical streams and identical font/ExtGState dicts.
    Repeats because merging (say) two ICC profiles can make the images that
    use them identical too. Returns {duplicate objgen: surviving objgen}."""
    merged = {}
    for _ in range(max_passes):
        seen, remap = {}, {}
        for obj in pdf.objects:
            if not _dedup_candidate(obj):
                continue
            try:
                k = _key(obj)
            except Exception:
                continue
            if k in seen:
                remap[obj.objgen] = seen[k]
            else:
                seen[k] = obj
        if not remap:
            break
        for obj in pdf.objects:
            if isinstance(obj, (Dictionary, Stream, Array)):
                _rewrite_refs(obj, remap)
        _rewrite_refs(pdf.trailer, remap)
        for dup, canon in remap.items():
            merged[dup] = canon.objgen
    # Resolve chains from later passes so every duplicate points at the survivor.
    for dup in merged:
        og = merged[dup]
        while og in merged:
            og = merged[og]
        merged[dup] = og
    return merged


# ---------------------------------------------------------------- metadata

_XMP_BLOAT = ("Thumbnails", "History", "Ingredients", "Manifest", "Pantry",
              "DocumentAncestors")
_XMP_BLOAT_RE = re.compile(
    r"<([\w-]+):(%s)\b[^>]*?(?:/>|>.*?</\1:\2\s*>)" % "|".join(_XMP_BLOAT), re.S)
_XMP_PADDING_RE = re.compile(rb"\s{64,}(?=<\?xpacket end)")


def _trim_xmp(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    trimmed = _XMP_BLOAT_RE.sub("", text).encode("utf-8")
    trimmed = _XMP_PADDING_RE.sub(b"\n", trimmed)
    try:
        ET.fromstring(trimmed)
    except ET.ParseError:
        return data
    return trimmed


def strip_private_data(pdf):
    """Remove PieceInfo (Illustrator/InDesign private data), per-object XMP,
    and the bulky parts of the document XMP (thumbnails, edit history).
    Document-level identity (title, PDF/A and PDF/X ids) is kept."""
    piece = objmeta = 0
    root_meta = pdf.Root.get("/Metadata")
    root_meta_og = root_meta.objgen if root_meta is not None and root_meta.is_indirect else None
    for obj in pdf.objects:
        if not isinstance(obj, (Dictionary, Stream)):
            continue
        if Name.PieceInfo in obj:
            del obj[Name.PieceInfo]
            piece += 1
        if obj.objgen != pdf.Root.objgen and Name.Metadata in obj:
            m = obj.get(Name.Metadata)
            if m is not None and m.is_indirect and m.objgen == root_meta_og:
                continue
            del obj[Name.Metadata]
            objmeta += 1
    if Name.PieceInfo in pdf.Root:
        del pdf.Root[Name.PieceInfo]
        piece += 1

    xmp_saved = 0
    if isinstance(root_meta, Stream):
        try:
            before = root_meta.read_bytes()
            after = _trim_xmp(before)
            if len(after) < len(before):
                root_meta.write(after)
                xmp_saved = len(before) - len(after)
        except Exception:
            pass
    return {"pieceinfo": piece, "object_xmp": objmeta, "xmp_bytes_trimmed": xmp_saved}


def save(pdf, path):
    pikepdf.settings.set_flate_compression_level(9)
    pdf.save(
        path,
        compress_streams=True,
        stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
        recompress_flate=True,
        object_stream_mode=pikepdf.ObjectStreamMode.generate,
        linearize=False,
    )

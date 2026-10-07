"""Compression pipeline: build a candidate, verify it, revert what fails,
fall back to lossless-only, and never return an unverified file."""

import os
import shutil
import tempfile
import time
from pathlib import Path

import pikepdf
import pymupdf

from . import images as img_mod
from . import precheck, structural, verify

# forms:  re-encode images drawn from inside Form XObjects (in place, same object)
# masked: re-encode the colour data of soft-masked images; the mask stays byte-identical
# resize_masks: when downsampling a masked image, resize its mask to the same grid
PRESETS = {
    "lossless": {"images": False},
    "balanced": {"images": True, "threshold": 300, "target": 300, "quality": 85, "subsampling": 0,
                 "forms": True, "masked": True},
    "max":      {"images": True, "threshold": 225, "target": 150, "quality": 80, "subsampling": 2,
                 "forms": True, "masked": True},
    # Sized per document for on-screen viewing: verified at the resolution the
    # page is shown at on a ~2000 px display, images kept at 2x that (retina).
    "screen":   {"images": True, "quality": 80, "subsampling": 2,
                 "forms": True, "masked": True, "resize_masks": True, "screen": True},
}
PRESET_LABELS = {"lossless": "Lossless", "balanced": "Balanced", "max": "Max", "screen": "Screen"}
SCREEN_PX = 2000
MIN_SAVING = 0.05
MAX_ROUNDS = 6


class UnsupportedPDF(Exception):
    pass


def output_path_for(src):
    src = Path(src)
    return src.with_name(f"{src.stem}_compressed.pdf")


def _check_supported(pdf):
    if pdf.is_encrypted:
        raise UnsupportedPDF("Encrypted PDF: rewriting it would drop its security settings.")
    acro = pdf.Root.get("/AcroForm")
    if acro is not None and int(acro.get("/SigFlags", 0)) & 1:
        raise UnsupportedPDF("Digitally signed PDF: any rewrite would invalidate the signature.")


def screen_settings(src):
    """Verification dpi and image target for the Screen preset, from the
    largest page: the page's long side fills SCREEN_PX pixels."""
    with pymupdf.open(src) as doc:
        long_in = max(max(p.rect.width, p.rect.height) for p in doc) / 72
    dpi = max(72, min(verify.DPI, round(SCREEN_PX / long_in)))
    target = min(300, 2 * dpi)
    return dpi, target


def preset_opts(preset, src):
    opts = dict(PRESETS[preset])
    if opts.get("screen"):
        _, target = screen_settings(src)
        opts.update(target=target, threshold=round(target * 1.1))
    return opts


def build(src, dst, preset, strip, exclude=frozenset(), progress=None, softened=frozenset()):
    opts = preset_opts(preset, src)
    softer = PRESETS["balanced"] if preset in ("max", "screen") else None
    with pikepdf.open(src) as pdf:
        _check_supported(pdf)
        stats = {"thumbnails_removed": structural.drop_thumbnails(pdf)}
        if strip:
            stats.update(structural.strip_private_data(pdf))
        merged = structural.deduplicate(pdf)
        stats["duplicates_merged"] = len(merged)
        results = img_mod.optimize_images(
            pdf, opts, exclude, (lambda i, n: progress("Optimizing images", i / max(n, 1)))
            if progress else None, softer, softened, str(src), merged)
        structural.save(pdf, dst)
    return stats, results


def _localize(orig_path, report, changed, dpi):
    """Pick the changed images responsible for each failing page: the ones whose
    on-page footprint overlaps a failing region; all changed images on the page
    when the failure is structural or can't be pinned down."""
    by_page = {}
    for r in changed:
        for p in r.pages:
            by_page.setdefault(p, []).append(r)
    revert, unexplained = set(), []
    doc = pymupdf.open(orig_path)
    try:
        for pg in report["pages"]:
            if pg["passed"]:
                continue
            cands = by_page.get(pg["page"], [])
            if not cands:
                unexplained.append(pg["page"])
                continue
            bad = pg.get("_bad_tiles")
            hits = set()
            if bad is not None and not pg["structure_failures"]:
                page = doc[pg["page"] - 1]
                to_px = page.rotation_matrix * pymupdf.Matrix(dpi / 72, dpi / 72)
                ids = {r.objgen[0]: r for r in cands}
                for info in page.get_image_info(xrefs=True):
                    r = ids.get(info.get("xref"))
                    if r is None:
                        continue
                    rect = pymupdf.Rect(info["bbox"]) * to_px
                    t0x = max(0, int(rect.x0 // verify.TILE) - 1)
                    t0y = max(0, int(rect.y0 // verify.TILE) - 1)
                    t1x = int(rect.x1 // verify.TILE) + 2
                    t1y = int(rect.y1 // verify.TILE) + 2
                    if bad[t0y:t1y, t0x:t1x].any():
                        hits.add(r.objgen)
            pool = hits or {r.objgen for r in cands}
            if len(pool) > 1 and not pg["structure_failures"]:
                # Several changed images overlap the failing area: undo the one
                # whose re-encode drifted furthest from the original first.
                score = {r.objgen: (r.score if r.score is not None else 0) for r in cands}
                pool = {min(pool, key=lambda og: score.get(og, 0))}
            revert |= pool
    finally:
        doc.close()
    return revert, unexplained


def _page_sizes(src):
    with pymupdf.open(src) as doc:
        return [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in doc]


def _failures(rep):
    out = [f"document: {c['check']} changed" for c in rep["doc_checks"] if not c["ok"]]
    out += [f"page {p['page']}: " + "; ".join(p["structure_failures"] + p["visual_failures"])
            for p in rep["pages"] if not p["passed"]]
    return out


def _public(report):
    """Strip internal arrays before the report leaves the engine."""
    out = dict(report)
    out["pages"] = [{k: v for k, v in p.items() if not k.startswith("_")} for p in report["pages"]]
    return out


def compress(src, preset="balanced", strip=True, out_path=None, progress=None, write=True):
    """Compress `src`. Writes `<name>_compressed.pdf` next to it (or `out_path`)
    only when the result passed verification and saves at least 5%."""
    t0 = time.time()
    src = Path(src)
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset {preset!r}")
    out_path = Path(out_path) if out_path else output_path_for(src)
    if out_path.resolve() == src.resolve():
        raise ValueError("Refusing to overwrite the original file.")
    say = progress or (lambda stage, frac: None)
    is_screen = bool(PRESETS[preset].get("screen"))
    vdpi = screen_settings(src)[0] if is_screen else verify.DPI
    gate = verify.SCREEN if is_screen else verify.STRICT
    orig_size = src.stat().st_size

    tmp = Path(tempfile.mkdtemp(prefix="pdfcompress-"))
    try:
        attempts, exclude, softened = [], set(), set()
        chosen = chosen_report = None
        stats = results = None
        can_ease = preset in ("max", "screen")
        tag = lambda ogs: [f"{og[0]} {og[1]} R" for og in sorted(ogs)]
        cand = tmp / "candidate.pdf"

        # 1. Build, then pre-check every image change on its own pages and
        #    rebuild without the ones that fail. Cheap compared with full rounds.
        cache = precheck.PageCache(src, vdpi, verify.doc_structure(src)["uses_cmyk"])
        try:
            passed_pre = set()
            for p in range(4):
                say("Optimizing" if p == 0 else "Re-optimizing after pre-check", 0)
                stats, results = build(src, cand, preset, strip, frozenset(exclude), say,
                                       frozenset(softened))
                pending = [r for r in results if r.objgen not in passed_pre]
                fails = precheck.check(src, pending, cache, gate,
                                       lambda i, n: say("Pre-checking images", i / max(n, 1)))
                passed_pre |= {r.objgen for r in pending if r.action == "recompressed"} - set(fails)
                if not fails:
                    break
                ease = {og for og in fails if can_ease and og not in softened}
                revert = set(fails) - ease
                softened |= ease
                exclude |= revert
                attempts.append({"round": "pre-check", "passed": False, "size": None,
                                 "failing_pages": sorted({int(w.split()[1].rstrip(":")) for w in fails.values()}),
                                 "eased": tag(ease), "reverted": tag(revert),
                                 "failures": [f"{og[0]} {og[1]} R on {w}" for og, w in sorted(fails.items())]})
            else:
                stats, results = build(src, cand, preset, strip, frozenset(exclude), say,
                                       frozenset(softened))
        finally:
            cache.close()

        # 2. Full verification. On a retry only the pages touched by a reverted
        #    image (plus the pages that failed) are checked again.
        rep, only = None, None
        for rnd in range(1, MAX_ROUNDS + 1):
            label = "Verifying" if rnd == 1 else f"Retry {rnd - 1}: verifying"
            part = verify.verify(src, cand, lambda i, n: say(label, i / max(n, 1)), vdpi, gate, only)
            if rep is None:
                rep = part
            else:
                fresh = {p["page"]: p for p in part["pages"]}
                rep["pages"] = [fresh.get(p["page"], p) for p in rep["pages"]]
                rep["doc_checks"] = part["doc_checks"]
                rep["passed"] = (all(c["ok"] for c in rep["doc_checks"])
                                 and all(p["passed"] for p in rep["pages"]))
            failing = [p["page"] for p in rep["pages"] if not p["passed"]]
            attempt = {"round": rnd, "passed": rep["passed"], "size": cand.stat().st_size,
                       "failing_pages": failing, "reverted": [], "failures": _failures(rep),
                       "pages_checked": len(part["pages"])}
            attempts.append(attempt)
            if rep["passed"]:
                chosen, chosen_report = cand, rep
                break
            changed = [r for r in results if r.action == "recompressed"]
            if not all(c["ok"] for c in rep["doc_checks"]) or not changed:
                break
            revert, _unexplained = _localize(src, rep, changed, vdpi)
            revert -= exclude
            if not revert:
                break
            ease = {og for og in revert if can_ease and og not in softened}
            revert -= ease
            softened |= ease
            attempt["eased"], attempt["reverted"] = tag(ease), tag(revert)
            exclude |= revert
            touched = {p for r in changed if r.objgen in (revert | ease) for p in r.pages}
            only = touched | set(failing)
            say(f"Retry {rnd}: re-optimizing", 0)
            stats, results = build(src, cand, preset, strip, frozenset(exclude), say,
                                   frozenset(softened))

        fallback = None
        if chosen is None and preset != "lossless":
            cand = tmp / "lossless.pdf"
            say("Falling back to lossless-only", 0)
            stats, results = build(src, cand, "lossless", strip, frozenset(), say)
            rep = verify.verify(src, cand, lambda i, n: say("Verifying lossless fallback", i / max(n, 1)), vdpi, gate)
            attempts.append({"round": "lossless fallback", "passed": rep["passed"],
                             "size": cand.stat().st_size,
                             "failing_pages": [p["page"] for p in rep["pages"] if not p["passed"]],
                             "reverted": [], "failures": _failures(rep)})
            fallback = "Image changes kept failing verification, so only lossless structural changes were applied."
            if rep["passed"]:
                chosen, chosen_report = cand, rep
            else:
                chosen_report = rep

        last_report = chosen_report if chosen_report is not None else rep
        new_size = chosen.stat().st_size if chosen else orig_size
        saved = orig_size - new_size
        outcome, reason, written = "compressed", None, None
        if chosen is None:
            outcome = "kept_original"
            reason = "No version passed verification, so the original is returned unchanged."
        elif saved < MIN_SAVING * orig_size:
            outcome = "kept_original"
            reason = (f"Compression saved only {saved / orig_size:.1%} (under 5%), "
                      "so the original is returned unchanged.")
        if outcome == "compressed" and write:
            shutil.copyfile(chosen, out_path)
            written = str(out_path)
        result_file = None
        if outcome == "compressed" and not write:
            result_file = tmp.parent / f"{tmp.name}-result.pdf"
            shutil.copyfile(chosen, result_file)

        report = {
            "file": src.name,
            "preset": preset,
            "strip_private_data": strip,
            "original_size": orig_size,
            "candidate_size": new_size,
            "final_size": new_size if outcome == "compressed" else orig_size,
            "saved_bytes": saved if outcome == "compressed" else 0,
            "saved_pct": round(100 * saved / orig_size, 1) if outcome == "compressed" else 0.0,
            "candidate_saved_pct": round(100 * saved / orig_size, 1),
            "outcome": outcome,
            "outcome_reason": reason,
            "fallback": fallback,
            "output_path": written,
            "result_file": str(result_file) if result_file else None,
            "structural": stats,
            "images": [r.to_dict() for r in results] if results else [],
            "attempts": attempts,
            "verification": _public(last_report),
            "page_sizes": _page_sizes(src),
            "elapsed_s": round(time.time() - t0, 1),
        }
        say("Done", 1)
        return report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

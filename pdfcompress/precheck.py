"""Per-image pre-check: before building the final file, apply each image
change on its own to the original and compare the pages it appears on.
Catches almost every change the full verification would reject, without a
full rebuild-and-verify round per suspect."""

import pymupdf

from . import verify

MAX_PAGES_PER_IMAGE = 3


def _apply(doc, n):
    x = n["xref"]
    doc.update_stream(x, n["data"], compress=False)
    doc.xref_set_key(x, "Filter", n["filter"])
    doc.xref_set_key(x, "DecodeParms", n["parms"] or "null")
    doc.xref_set_key(x, "Width", str(n["w"]))
    doc.xref_set_key(x, "Height", str(n["h"]))


class PageCache:
    """Renders of the original's pages, reused across images and rounds."""

    def __init__(self, src, dpi, cmyk):
        self.doc = pymupdf.open(src)
        self.dpi, self.cmyk, self.cache = dpi, cmyk, {}

    def get(self, i):
        if i not in self.cache:
            page = self.doc[i]
            self.cache[i] = (verify.render(page, pymupdf.csRGB, self.dpi),
                             verify.render(page, pymupdf.csCMYK, self.dpi) if self.cmyk else None)
        return self.cache[i]

    def close(self):
        self.doc.close()


def check(src, results, cache, gate, progress=None):
    """Return {objgen: failure text} for changes that fail on their own pages."""
    todo = [r for r in results if r.action == "recompressed" and getattr(r, "_new", None)]
    failed = {}
    for k, r in enumerate(todo):
        if progress:
            progress(k, len(todo))
        doc = pymupdf.open(src)
        try:
            _apply(doc, r._new)
            if r._new["mask"]:
                _apply(doc, r._new["mask"])
            for pno in r.pages[:MAX_PAGES_PER_IMAGE]:
                i = pno - 1
                rgb0, cmyk0 = cache.get(i)
                page = doc[i]
                m = verify.compare(rgb0, verify.render(page, pymupdf.csRGB, cache.dpi), t=gate)
                bad = (m["ssim"] < gate["ssim_page_min"] or m["ssim_tile_min"] < gate["ssim_region_min"]
                       or m["de_mean"] >= gate["de_mean_max"] or m["de_tile_max"] > gate["de_region_max"])
                if not bad and cmyk0 is not None:
                    c = verify.compare(cmyk0, verify.render(page, pymupdf.csCMYK, cache.dpi),
                                       with_de=False, t=gate)
                    bad = c["ssim"] < gate["ssim_page_min"] or c["ssim_tile_min"] < gate["ssim_region_min"]
                if bad:
                    failed[r.objgen] = (f"page {pno}: SSIM {m['ssim']:.4f}, local {m['ssim_tile_min']:.3f}, "
                                        f"ΔE region {m['de_tile_max']:.2f}")
                    break
        except Exception as e:      # can't pre-check it → leave it to the full verification
            pass
        finally:
            doc.close()
    if progress:
        progress(len(todo), len(todo))
    return failed

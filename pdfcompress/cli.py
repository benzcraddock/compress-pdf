"""Command line: python -m pdfcompress FILE.pdf [...] [--preset balanced]"""

import argparse
import json
import sys

from .engine import PRESETS, UnsupportedPDF, compress


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def format_report(r):
    v = r["verification"]
    lines = [f"━━ {r['file']}  ({r['preset']})"]
    if r["outcome"] == "compressed":
        lines.append(f"   {human(r['original_size'])} → {human(r['final_size'])}   "
                     f"saved {r['saved_pct']}%   → {r['output_path'] or 'in memory'}")
    else:
        lines.append(f"   {human(r['original_size'])}   original kept: {r['outcome_reason']}")
        lines.append(f"   (best verified candidate was {human(r['candidate_size'])}, "
                     f"{r['candidate_saved_pct']}% smaller)")
    if r["fallback"]:
        lines.append(f"   ⚠ {r['fallback']}")
    s = r["structural"] or {}
    lines.append("   Structural: " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in s.items()))

    rec = [i for i in r["images"] if i["action"] == "recompressed"]
    other = [i for i in r["images"] if i["action"] != "recompressed"]
    lines.append(f"   Images: {len(rec)} recompressed, {len(other)} left as-is")
    for i in rec:
        lines.append(f"     ✓ {i['id']:>10}  p{','.join(map(str, i['pages']))}  {i['width']}×{i['height']}"
                     f"→{i['new_width']}×{i['new_height']}  {i['colorspace']}  "
                     f"{human(i['bytes_before'])}→{human(i['bytes_after'])}  {i['reason']}")
    reasons = {}
    for i in other:
        reasons.setdefault(i["reason"], []).append(i["id"])
    for reason, ids in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"     · {len(ids):>3} × {reason}")

    for a in r["attempts"]:
        if not a["passed"] or a["reverted"] or a.get("eased"):
            eased = f"  eased to Balanced {a['eased']}" if a.get("eased") else ""
            lines.append(f"   Round {a['round']}: {'pass' if a['passed'] else 'FAIL'} "
                         f"pages {a['failing_pages']}  reverted {a['reverted'] or '-'}{eased}")
            for f in a.get("failures", []):
                lines.append(f"       {f}")

    lines.append(f"   Verification: {'PASSED' if v['passed'] else 'FAILED'}"
                 f"  ({v['thresholds']['dpi']} dpi, RGB{' + CMYK' if v['cmyk_checked'] else ''}, "
                 f"{'screen-viewing' if v['thresholds'].get('mode') == 'screen' else 'identical-render'} thresholds)")
    bad_doc = [c["check"] for c in v["doc_checks"] if not c["ok"]]
    lines.append("   Document checks: " + ("all identical" if not bad_doc else "CHANGED: " + ", ".join(bad_doc)))
    lines.append("   Page  Result  SSIM     min-region  ΔE mean  ΔE region  " +
                 ("CMYK SSIM  " if v["cmyk_checked"] else "") + "Notes")
    for p in v["pages"]:
        notes = "identical render" if p["identical"] else "; ".join(p["structure_failures"] + p["visual_failures"])
        cm = f"{p['cmyk_ssim']:.5f}    " if v["cmyk_checked"] else ""
        lines.append(f"   {p['page']:>4}  {'pass' if p['passed'] else 'FAIL':6}  {p['ssim']:.5f}  "
                     f"{p['ssim_tile_min']:.4f}      {p['de_mean']:.3f}    {p['de_tile_max']:.2f}       "
                     f"{cm}{notes}")
    lines.append(f"   ({r['elapsed_s']} s)")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pdfcompress", description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--preset", choices=list(PRESETS), default="balanced")
    ap.add_argument("--keep-private-data", action="store_true",
                    help="Keep XMP history/thumbnails and PieceInfo (stripped by default)")
    ap.add_argument("--json", metavar="PATH", help="Also write the full reports as JSON")
    args = ap.parse_args(argv)

    reports, code = [], 0
    for f in args.files:
        def progress(stage, frac, _tty=sys.stderr.isatty()):
            if not _tty:
                return
            print(f"\r  {stage:<36} {frac:4.0%}", end="", file=sys.stderr, flush=True)
        try:
            r = compress(f, args.preset, not args.keep_private_data, progress=progress)
        except (UnsupportedPDF, ValueError, OSError) as e:
            print(f"\r━━ {f}: {e}", file=sys.stderr)
            code = 1
            continue
        if sys.stderr.isatty():
            print("\r" + " " * 48 + "\r", end="", file=sys.stderr)
        print(format_report(r))
        reports.append(r)
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(reports, fh, indent=2)
    return code

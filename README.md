# PDF Compressor

A local PDF compressor that never changes how a page looks. Each output is rendered
page by page and compared with the original before you get it back. If a page
doesn't match, the change responsible is reverted.

It runs entirely on your machine, in the same suite as Lottie Checker.

## Usage

```sh
./compress-pdf                       # web app at http://127.0.0.1:8765
./compress-pdf a.pdf b.pdf           # command line → a_compressed.pdf next to a.pdf
./compress-pdf a.pdf --preset max --json report.json
```

The first run creates `.venv/` and installs `requirements.txt`.

The command line writes `<name>_compressed.pdf` next to the original. The web app can't
see where a dropped file came from, so it gives you a download with the same name instead.
The original is never overwritten. If the saving is under 5%, or nothing passes
verification, no file is written and the report explains why.

## Presets

| Preset | Images |
| --- | --- |
| **Lossless** | Never recompressed. Structural work only. |
| **Balanced** | Downsample above 300 → 300 ppi. Photos JPEG q85 (4:4:4). Graphics Flate. |
| **Max** | Downsample above 225 → 150 ppi. Photos JPEG q80. An image that fails verification is retried at Balanced settings before being reverted. |
| **Screen** | For decks viewed on screen. Sized per document so the page's long side fills a ~2000 px display; images are kept at 2× that (retina), and soft masks are resized with their images. Verified at that on-screen size with looser "no visible difference" thresholds (below). |

Every preset also runs these steps:
- Flate-recompresses streams and generates object streams.
- Merges byte-identical images, fonts and ICC profiles.
- Drops unreachable objects and page thumbnails.
- With **Strip private data** on (the default), removes PieceInfo, per-object XMP, and
  XMP thumbnails and edit history.

## What is never touched

- Soft masks, in every preset except Screen. A masked image's colour data may be re-encoded at
  its original size, but the mask itself stays byte-identical. Screen may resize a mask together
  with its image, to the same pixel grid, when no other image shares it.
- Images with a colour-key /Mask or JPEG 2000 alpha.
- Images used inside patterns, Type3 fonts or annotations. Images inside Form XObjects are
  re-encoded in place on the same object, so placement can't change.
- Images in Indexed, Separation, DeviceN or Lab colour spaces.
- Images that aren't 8-bit.
- JBIG2, CCITT and JPEG 2000 images.
- JPEGs that don't need downsampling.

A recompressed image keeps its colour space entry, ICC profile and `/Decode` exactly.
Only its stream data changes, in place on the same object, so every placement matrix
and BBox stays put.

Ghostscript is not used. Fonts, vectors, transparency, layers, links, bookmarks, form
fields, annotations, overprint and spot colours are left as they are.

Encrypted and digitally signed PDFs are refused. A rewrite would drop the security
settings or invalidate the signature.

## Verification

Both files are rendered with MuPDF at 150 dpi in RGB. If the file uses CMYK, an ICC
OutputIntent or spot colours, they are rendered in CMYK as well. A page passes when:

- SSIM ≥ 0.995, and every 16 px region (≈2.7 mm) has SSIM ≥ 0.95
- Mean ΔE2000 < 1.0, and no 16 px region averages above 3.0
- These are identical:
  - page boxes, rotation, image placement boxes, fonts, annotations and links
  - page count, bookmarks, layers and form fields
  - spot colour names, overprint settings, blend modes and transparency groups
  - OutputIntent profiles

Screen uses the same checks at on-screen resolution with looser thresholds: SSIM ≥ 0.985,
region SSIM ≥ 0.70, mean ΔE < 1.5, region ΔE ≤ 6. Structural checks are unchanged.

When a page fails, the changed images whose footprint overlaps the failing regions are found.
If several overlap, the one whose re-encode drifted furthest from the original is reverted
first, then everything is verified again. If it still fails, the lossless-only version is
verified instead. A file that fails verification is never returned.

## Design

`web/suite.css` is a verbatim copy of `lottie-checker/styles.css`: tokens, Inter, header,
buttons, drop zone, selects and checkboxes. Keep it identical across the suite.
PDF-specific components (file cards, badges, report tables) are in `web/app.css` and
use only those tokens. Like Lottie Checker, the app is dark-only.

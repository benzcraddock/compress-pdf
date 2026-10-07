"""Local web UI: python -m pdfcompress.server  →  http://127.0.0.1:8765

Binds to 127.0.0.1 only. Uploaded PDFs go to a private temp folder for the
life of the server and are deleted on exit; nothing leaves the machine.
"""

import argparse
import json
import queue
import re
import shutil
import signal
import tempfile
import threading
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import pymupdf

from .engine import PRESETS, UnsupportedPDF, compress

WEB = Path(__file__).resolve().parent.parent / "web"
WORK = Path(tempfile.mkdtemp(prefix="pdfcompress-server-"))
JOBS = {}
QUEUE = queue.Queue()
LOCK = threading.Lock()


class Cancelled(Exception):
    pass


def _worker():
    while True:
        job_id = QUEUE.get()
        job = JOBS[job_id]
        if job.get("cancel"):
            continue

        def progress(stage, frac, _job=job):
            if _job.get("cancel"):
                raise Cancelled()
            with LOCK:
                _job.update(state="running", stage=stage, progress=round(frac, 3))
        try:
            report = compress(job["path"], job["preset"], job["strip"], progress=progress, write=False)
            if report.get("result_file"):
                dest = Path(job["path"]).with_name(f"result-{job_id}.pdf")
                shutil.move(report["result_file"], dest)
                report["result_file"] = str(dest)
            with LOCK:
                job.update(state="done", stage="Done", progress=1, report=report)
        except Cancelled:
            with LOCK:
                job.update(state="cancelled", stage="Cancelled")
        except UnsupportedPDF as e:
            with LOCK:
                job.update(state="error", error=str(e))
        except Exception as e:  # report, don't crash the worker
            with LOCK:
                job.update(state="error", error=f"Could not process this PDF ({e.__class__.__name__}: {e})")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, *a):
        pass

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_error(403, "Local access only")
            return False
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        pm = re.fullmatch(r"/api/jobs/([\w-]+)/page/(\d+)", url.path)
        if pm:
            return self._page(pm.group(1), int(pm.group(2)), parse_qs(url.query))
        m = re.fullmatch(r"/api/jobs/([\w-]+)(/download)?", url.path)
        if not m:
            return super().do_GET()
        job = JOBS.get(m.group(1))
        if job is None:
            return self._json({"error": "unknown job"}, 404)
        if not m.group(2):
            with LOCK:
                return self._json({k: v for k, v in job.items() if k != "path"})
        rf = (job.get("report") or {}).get("result_file")
        if job.get("state") != "done" or not rf:
            return self._json({"error": "no compressed file for this job"}, 404)
        data = Path(rf).read_bytes()
        name = Path(job["name"]).stem + "_compressed.pdf"
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(name)}")
        self.end_headers()
        self.wfile.write(data)

    def _page(self, job_id, pno, q):
        """Render one page of the original or the compressed file as PNG."""
        job = JOBS.get(job_id)
        if job is None:
            return self._json({"error": "unknown job"}, 404)
        which = q.get("which", ["new"])[0]
        path = job["path"] if which == "orig" else (job.get("report") or {}).get("result_file")
        if not path:
            return self._json({"error": "no compressed file"}, 404)
        width = max(200, min(4000, int(q.get("w", ["1200"])[0])))
        with pymupdf.open(path) as doc:
            if not 1 <= pno <= doc.page_count:
                return self._json({"error": "no such page"}, 404)
            page = doc[pno - 1]
            zoom = width / page.rect.width
            png = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).tobytes("png")
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(png)

    def do_DELETE(self):
        if not self._host_ok():
            return
        m = re.fullmatch(r"/api/jobs/([\w-]+)", urlparse(self.path).path)
        job = JOBS.get(m.group(1)) if m else None
        if job is None:
            return self._json({"error": "unknown job"}, 404)
        with LOCK:
            if job["state"] in ("queued", "running"):
                job["cancel"] = True
        return self._json({"ok": True})

    def do_POST(self):
        if not self._host_ok():
            return
        url = urlparse(self.path)
        if url.path != "/api/jobs":
            return self._json({"error": "not found"}, 404)
        q = parse_qs(url.query)
        name = Path(unquote(q.get("name", ["file.pdf"])[0])).name or "file.pdf"
        preset = q.get("preset", ["balanced"])[0]
        if preset not in PRESETS:
            return self._json({"error": "bad preset"}, 400)
        strip = q.get("strip", ["1"])[0] != "0"
        job_id = uuid.uuid4().hex[:12]
        src_job = JOBS.get(q.get("from", [""])[0])
        if src_job is not None:
            # Re-run an already uploaded file with different settings.
            with LOCK:
                JOBS[job_id] = {"id": job_id, "name": src_job["name"], "path": src_job["path"],
                                "preset": preset, "strip": strip, "state": "queued",
                                "stage": "Queued", "progress": 0}
            QUEUE.put(job_id)
            return self._json({"id": job_id})
        length = int(self.headers.get("Content-Length", 0))
        folder = WORK / job_id
        folder.mkdir()
        path = folder / name
        with open(path, "wb") as fh:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        if not path.read_bytes()[:1024].lstrip().startswith(b"%PDF"):
            shutil.rmtree(folder, ignore_errors=True)
            return self._json({"error": "Not a PDF file"}, 400)
        with LOCK:
            JOBS[job_id] = {"id": job_id, "name": name, "path": str(path), "preset": preset,
                            "strip": strip, "state": "queued", "stage": "Queued", "progress": 0}
        QUEUE.put(job_id)
        return self._json({"id": job_id})


def main():
    ap = argparse.ArgumentParser(prog="pdfcompress.server")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    # Treat `kill`/`pkill` like Ctrl+C so uploaded files are always cleaned up.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    threading.Thread(target=_worker, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"PDF Compressor running at {url}  (Ctrl+C to stop)")
    if not args.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()

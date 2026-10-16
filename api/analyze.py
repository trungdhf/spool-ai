import base64
import glob
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from spool.pipeline import run  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            files = [(f["name"], base64.b64decode(f["data"])) for f in body.get("files", [])]
            if not files:
                files = [(os.path.basename(p), open(p, "rb").read())
                         for p in sorted(glob.glob(os.path.join(ROOT, "samples", "*.pdf")))]
            out = json.dumps(run(files, body.get("ai", True)), ensure_ascii=False, default=str).encode()
            self.send_response(200)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            out = json.dumps({"error": str(e)}).encode()
            self.send_response(500)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

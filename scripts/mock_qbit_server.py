"""最小 mock qBittorrent WebAPI 服务(仅用于端到端验证客户端链路, 非真实 qbit)。
纯标准库(http.server), 无需额外依赖。
实现: /api/v2/auth/login(Set-Cookie SID) /app/webapiVersion /transfer/info
      /torrents/info(带 ma: tag 的任务, 含做种+下载中, 验证集数聚合)。
运行: venv/bin/python scripts/mock_qbit_server.py [port]
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
VALID_USER, VALID_PWD = "admin", "mockpw"
SID = "mocksid123"

TORRENTS = [
    {"name": "One.Piece.S01E01.1080p.mkv", "hash": "h1", "tags": "ma:tv:37854",
     "state": "uploading", "size": 1073741824, "progress": 1.0,
     "dlspeed": 0, "upspeed": 500000, "num_seeds": 5, "num_leechs": 0},
    {"name": "One.Piece.S01E02.1080p.mkv", "hash": "h2", "tags": "ma:tv:37854",
     "state": "downloading", "size": 1073741824, "progress": 0.45,
     "dlspeed": 1048576, "upspeed": 0, "num_seeds": 2, "num_leechs": 1},
    {"name": "One.Piece.S01E03.1080p.mkv", "hash": "h3", "tags": "ma:tv:37854,hd",
     "state": "queuedDownloading", "size": 1073741824, "progress": 0.0,
     "dlspeed": 0, "upspeed": 0, "num_seeds": 0, "num_leechs": 0},
    {"name": "误杀2.2021.2160p.mkv", "hash": "h4", "tags": "ma:movie:899665",
     "state": "downloading", "size": 5368709120, "progress": 0.3,
     "dlspeed": 2097152, "upspeed": 0, "num_seeds": 1, "num_leechs": 3},
    {"name": "Random.NoTag", "hash": "h5", "tags": "", "state": "uploading",
     "size": 100, "progress": 1.0, "dlspeed": 0, "upspeed": 0,
     "num_seeds": 1, "num_leechs": 0},
]
TRANSFER = {"download_payload_rate": 2097152, "upload_payload_rate": 524288,
            "total_downloaded": 1073741824, "total_uploaded": 536870912}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        return (self.headers.get("Cookie") or "").find(f"SID={SID}") != -1

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _txt(self, text, code=200, set_sid=False):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        if set_sid:
            self.send_header("Set-Cookie", f"SID={SID}; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/v2/auth/login":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8")
            form = dict(kv.split("=", 1) for kv in raw.split("&") if "=" in kv)
            if form.get("username") == VALID_USER and form.get("password") == VALID_PWD:
                self._txt("Ok.", 200, set_sid=True)
            else:
                self._txt("Fails.", 403)
            return
        if not self._auth():
            self._txt("Fails.", 403)
            return
        self._txt("Ok.")

    def do_GET(self):
        if not self.path.startswith("/api/v2"):
            self._txt("not found", 404)
            return
        if not self._auth():
            self._txt("Fails.", 403)
            return
        if self.path.startswith("/api/v2/app/webapiVersion"):
            self._txt("v2.0.0 (mock)")
        elif self.path.startswith("/api/v2/transfer/info"):
            self._json(TRANSFER)
        elif self.path.startswith("/api/v2/torrents/info"):
            self._json(TORRENTS)
        else:
            self._txt("not found", 404)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"mock qbit on 127.0.0.1:{PORT} (admin/mockpw)")
    srv.serve_forever()

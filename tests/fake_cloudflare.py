"""A fake Cloudflare API for tests/access_201.yml: Access policies, Access
apps and DNS records kept in memory. A POST answers 201 Created, as the real
API does for Access; GET /_writes counts the POSTs and PUTs so far.

    python3 tests/fake_cloudflare.py PORT
"""
import json
import sys
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

store = {}  # collection path -> {id: object}
writes = 0


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def split(self):
        url = urlsplit(self.path)
        parts = url.path.rstrip("/").split("/")
        # .../access/policies, .../access/apps, .../dns_records [/id]
        if parts[-1] in ("policies", "apps", "dns_records"):
            return "/".join(parts), None, parse_qs(url.query)
        return "/".join(parts[:-1]), parts[-1], parse_qs(url.query)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def do_GET(self):
        if self.path == "/_writes":
            return self.reply(200, {"writes": writes})
        coll, _, query = self.split()
        items = list(store.get(coll, {}).values())
        if "name" in query:
            items = [i for i in items if i["name"] == query["name"][0]]
        self.reply(200, {"success": True, "result": items})

    def do_POST(self):
        global writes
        writes += 1
        coll, _, _ = self.split()
        obj = dict(self.body(), id=uuid.uuid4().hex)
        store.setdefault(coll, {})[obj["id"]] = obj
        self.reply(201, {"success": True, "result": obj})

    def do_PUT(self):
        global writes
        writes += 1
        coll, oid, _ = self.split()
        obj = dict(self.body(), id=oid)
        store.setdefault(coll, {})[oid] = obj
        self.reply(200, {"success": True, "result": obj})

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()

#!/usr/bin/env python3
"""preview-access: Traefik forward-auth that asks for a Cloudflare Access token.

Traefik sends every request on its entrypoints here before routing it
(forwardAuth). A request to a host in one of the protected zones passes only
with a valid Cf-Access-Jwt-Assertion header: signed (RS256) by one of the
team's Access keys, issued by the team domain, for that zone's Access
application (aud), and not expired. Cloudflare adds that header to every
request it lets through Access; a request made on this host straight to
Traefik has none. Every other host passes untouched.

Answers 200 (pass) or 403 (refused), which Traefik returns as is.

    python3 preview-access.py config.json

config.json, written by the preview_access role:

    {"listen": ["0.0.0.0", 9180],
     "issuer": "https://<team>.cloudflareaccess.com",
     "zones": {"example.dev": ["<aud tag of the zone's Access application>"]}}

Standard library only. The keys come from <issuer>/cdn-cgi/access/certs,
fetched at start, every hour, and again when a token names an unknown key
(Access rotates them). The RSA check is PKCS#1 v1.5 with SHA-256: the
signature is raised to the key's exponent and the result compared with the
whole expected encoding, never parsed.
"""
import base64
import binascii
import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Clock difference allowed on exp and nbf, in seconds.
LEEWAY = 60
# Keys are fetched this often, and at most this often for an unknown key;
# after a failed fetch, again RETRY_AFTER seconds later.
REFRESH_EVERY = 3600
REFETCH_AFTER = 30
RETRY_AFTER = 5
FETCH_TIMEOUT = 10
# Access tokens are about 1 KB; anything far larger is refused unread.
MAX_TOKEN = 16384
# DER prefix of a SHA-256 DigestInfo (RFC 8017, section 9.2).
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")

DENIED = b"Forbidden: this preview needs a Cloudflare Access login.\n"


def log(message):
    print(message, file=sys.stderr, flush=True)


def b64url(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def rsa_sha256_verify(n, e, message, signature):
    """True when `signature` is a PKCS#1 v1.5 SHA-256 signature of `message`."""
    k = (n.bit_length() + 7) // 8
    if n.bit_length() < 2048 or len(signature) != k:
        return False
    s = int.from_bytes(signature, "big")
    if s >= n:
        return False
    encoded = pow(s, e, n).to_bytes(k, "big")
    t = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    expected = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return hmac.compare_digest(encoded, expected)


def fetch_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "preview-access"})
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return json.load(response)


def parse_keys(certs):
    """{kid: (n, e)} for the RSA keys of an Access certs document."""
    keys = {}
    for key in certs.get("keys", []):
        if key.get("kty") != "RSA" or not isinstance(key.get("kid"), str):
            continue
        keys[key["kid"]] = (int.from_bytes(b64url(key["n"]), "big"),
                            int.from_bytes(b64url(key["e"]), "big"))
    return keys


class Keys:
    """The team's signing keys, refreshed from its certs URL."""

    def __init__(self, url, fetch=fetch_json, clock=time.monotonic):
        self.url = url
        self.fetch = fetch
        self.clock = clock
        self.keys = {}
        # When an unknown key may make it fetch again; a failed fetch counts
        # too, so a broken endpoint is not hammered.
        self.next_fetch = None
        self.ok = False
        self.lock = threading.Lock()

    def refresh(self, unless_recent=False):
        """Fetch the keys; True when that worked. Keeps the old ones otherwise."""
        with self.lock:
            if unless_recent and self.next_fetch is not None and self.clock() < self.next_fetch:
                return self.ok
            self.ok = False
            try:
                keys = parse_keys(self.fetch(self.url))
            except Exception as error:  # noqa: BLE001 - any failure keeps the old keys
                log(f"could not fetch the Access keys from {self.url}: {error}")
                keys = {}
            else:
                if not keys:
                    log(f"no RSA keys at {self.url}; keeping the previous ones")
            if keys:
                self.keys = keys
                self.ok = True
            self.next_fetch = self.clock() + (REFETCH_AFTER if self.ok else RETRY_AFTER)
            return self.ok

    def get(self, kid):
        key = self.keys.get(kid)
        if key is None:
            self.refresh(unless_recent=True)
            key = self.keys.get(kid)
        return key


def normalize_host(host):
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host  # an IPv6 literal, never in a zone
    return host.rsplit(":", 1)[0].rstrip(".")


def protected_zone(host, zones):
    """The zone `host` belongs to, or None. The zone's apex counts too."""
    for zone in zones:
        if host == zone or host.endswith("." + zone):
            return zone
    return None


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check(host, token, zones, issuer, keys, now):
    """(allowed, reason) for a request to `host` carrying `token`."""
    zone = protected_zone(normalize_host(host), zones)
    if zone is None:
        return True, "not protected"
    if not token:
        return False, "no Cf-Access-Jwt-Assertion header"
    if len(token) > MAX_TOKEN:
        return False, "token too large"
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = (header_b64 + "." + payload_b64).encode("ascii")
        header = json.loads(b64url(header_b64))
        payload = json.loads(b64url(payload_b64))
        signature = b64url(signature_b64)
    except (ValueError, binascii.Error, UnicodeError):
        return False, "malformed token"
    if not isinstance(header, dict) or not isinstance(payload, dict):
        return False, "malformed token"
    if header.get("alg") != "RS256":
        return False, "algorithm is not RS256"
    kid = header.get("kid")
    key = keys.get(kid) if isinstance(kid, str) else None
    if key is None:
        return False, "unknown signing key"
    if not rsa_sha256_verify(*key, signing_input, signature):
        return False, "bad signature"
    if payload.get("iss") != issuer:
        return False, "wrong issuer"
    aud = payload.get("aud")
    auds = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if not set(a for a in auds if isinstance(a, str)) & set(zones[zone]):
        return False, "token is for another Access application"
    exp = payload.get("exp")
    if not is_number(exp) or exp + LEEWAY < now:
        return False, "token expired"
    nbf = payload.get("nbf")
    if nbf is not None and (not is_number(nbf) or nbf - LEEWAY > now):
        return False, "token not valid yet"
    return True, "valid token"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "preview-access"
    sys_version = ""

    def answer(self, send_body):
        host = self.headers.get("X-Forwarded-Host", "")
        try:
            allowed, reason = check(host, self.headers.get("Cf-Access-Jwt-Assertion"),
                                    self.server.zones, self.server.issuer,
                                    self.server.keys, time.time())
        except Exception as error:  # noqa: BLE001 - refuse rather than drop the connection
            allowed, reason = False, f"error: {error!r}"
        if allowed:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        log(f"refused {normalize_host(host)}: {reason}")
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(DENIED)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(DENIED)

    # Traefik asks with GET whatever the original method was.
    def do_GET(self):
        self.answer(send_body=True)

    def do_HEAD(self):
        self.answer(send_body=False)

    def log_message(self, format, *args):
        pass  # every request of every site passes here; only refusals are logged


def serve(config, keys=None):
    issuer = config["issuer"].rstrip("/")
    server = ThreadingHTTPServer(tuple(config["listen"]), Handler)
    server.daemon_threads = True
    server.issuer = issuer
    server.zones = {zone.lower(): list(auds) for zone, auds in config["zones"].items()}
    server.keys = keys or Keys(issuer + "/cdn-cgi/access/certs")
    return server


def refresh_forever(keys):
    while True:
        time.sleep(REFRESH_EVERY if keys.refresh() else RETRY_AFTER)


def main():
    with open(sys.argv[1]) as file:
        config = json.load(file)
    server = serve(config)
    threading.Thread(target=refresh_forever, args=(server.keys,), daemon=True).start()
    log(f"protecting {', '.join(sorted(server.zones))} with {server.issuer}")
    server.serve_forever()


if __name__ == "__main__":
    main()

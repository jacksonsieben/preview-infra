#!/usr/bin/env python3
"""preview-access: which requests Traefik lets through on the protected zones.

    python3 tests/preview_access.py

Loads roles/preview_access/files/preview-access.py as it is installed. The
Access keys are RSA keys made with `openssl` in a temporary directory, and
tokens are signed with them the way Cloudflare signs its own. Offline: the
certs endpoint is a function, and the HTTP check runs on 127.0.0.1.
"""
import base64
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "roles/preview_access/files/preview-access.py")

sys.dont_write_bytecode = True
loader = importlib.machinery.SourceFileLoader("preview_access", SCRIPT)
spec = importlib.util.spec_from_loader("preview_access", loader)
pa = importlib.util.module_from_spec(spec)
loader.exec_module(pa)

ISSUER = "https://team.cloudflareaccess.com"
AUD = "aud-example-dev"
OTHER_AUD = "aud-other-zone"
ZONES = {"example.dev": [AUD], "other.dev": [OTHER_AUD]}
NOW = 1_800_000_000


def check(name, got, expected):
    assert got == expected, f"{name}: got {got}, expected {expected}"
    print(f"ok  {name}")


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Key:
    def __init__(self, directory, name, bits=2048):
        self.kid = name
        self.path = os.path.join(directory, name + ".pem")
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                        f"rsa_keygen_bits:{bits}", "-out", self.path],
                       check=True, capture_output=True)
        modulus = subprocess.run(["openssl", "rsa", "-in", self.path, "-noout", "-modulus"],
                                 check=True, capture_output=True, text=True).stdout
        self.n = int(modulus.strip().split("=", 1)[1], 16)

    def jwk(self):
        n = self.n.to_bytes((self.n.bit_length() + 7) // 8, "big")
        return {"kid": self.kid, "kty": "RSA", "alg": "RS256", "use": "sig",
                "e": b64url((65537).to_bytes(3, "big")), "n": b64url(n)}

    def sign(self, data):
        return subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.path],
                              input=data, check=True, capture_output=True).stdout

    def token(self, header=None, **claims):
        header = {"alg": "RS256", "kid": self.kid, "typ": "JWT"} | (header or {})
        payload = {"aud": [AUD], "email": "you@example.com", "exp": NOW + 3600,
                   "iat": NOW - 10, "nbf": NOW - 10, "iss": ISSUER, "type": "app",
                   "sub": "0b5f4c1e"} | claims
        payload = {k: v for k, v in payload.items() if v is not None}
        signing_input = (b64url(json.dumps(header).encode()) + "."
                         + b64url(json.dumps(payload).encode()))
        return signing_input + "." + b64url(self.sign(signing_input.encode()))


class FakeCerts:
    """The certs endpoint: serves `keys`, counts the calls."""

    def __init__(self, *keys):
        self.keys = list(keys)
        self.calls = 0
        self.fail = False

    def __call__(self, url):
        assert url == ISSUER + "/cdn-cgi/access/certs", url
        self.calls += 1
        if self.fail:
            raise OSError("certs endpoint down")
        return {"keys": [k.jwk() for k in self.keys], "public_cert": {}, "public_certs": []}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def run(keys, host, token, now=NOW):
    return pa.check(host, token, ZONES, ISSUER, keys, now)


def test_check(key, rotated, stranger):
    certs = FakeCerts(key)
    keys = pa.Keys(ISSUER + "/cdn-cgi/access/certs", fetch=certs, clock=Clock())
    good = key.token()

    check("domain previews pass without a token", run(keys, "pr-36-preview.example.com", None),
          (True, "not protected"))
    check("production passes without a token", run(keys, "example.com", None), (True, "not protected"))
    check("a name ending like the zone is another zone", run(keys, "badexample.dev", None),
          (True, "not protected"))
    check("no Host passes (no Host rule can match it)", run(keys, "", None), (True, "not protected"))
    check("no certs fetched for unprotected hosts", certs.calls, 0)

    for host in ["main.example.dev", "pr-12.example.dev", "MAIN.Example.DEV",
                 "main.example.dev:80", "main.example.dev.", "example.dev", "a.b.example.dev"]:
        check(f"{host}: refused without a token", run(keys, host, None),
              (False, "no Cf-Access-Jwt-Assertion header"))
    check("valid token passes", run(keys, "main.example.dev", good), (True, "valid token"))
    check("certs fetched once", certs.calls, 1)
    check("aud as a string passes", run(keys, "main.example.dev", key.token(aud=AUD)),
          (True, "valid token"))
    check("valid token on a host with a port", run(keys, "pr-1.example.dev:80", good),
          (True, "valid token"))

    check("token of the other zone's application",
          run(keys, "main.example.dev", key.token(aud=[OTHER_AUD])),
          (False, "token is for another Access application"))
    check("this zone's token on the other zone", run(keys, "main.other.dev", good),
          (False, "token is for another Access application"))
    check("no aud", run(keys, "main.example.dev", key.token(aud=None)),
          (False, "token is for another Access application"))
    check("expired", run(keys, "main.example.dev", key.token(exp=NOW - 61)), (False, "token expired"))
    check("expired within the leeway", run(keys, "main.example.dev", key.token(exp=NOW - 30)),
          (True, "valid token"))
    check("no exp", run(keys, "main.example.dev", key.token(exp=None)), (False, "token expired"))
    check("exp not a number", run(keys, "main.example.dev", key.token(exp="9999999999")),
          (False, "token expired"))
    check("not valid yet", run(keys, "main.example.dev", key.token(nbf=NOW + 120)),
          (False, "token not valid yet"))
    check("another team", run(keys, "main.example.dev", key.token(iss="https://evil.cloudflareaccess.com")),
          (False, "wrong issuer"))

    check("alg none", run(keys, "main.example.dev", key.token(header={"alg": "none"})),
          (False, "algorithm is not RS256"))
    check("alg HS256", run(keys, "main.example.dev", key.token(header={"alg": "HS256"})),
          (False, "algorithm is not RS256"))
    head, payload, signature = good.split(".")
    forged = json.loads(base64.urlsafe_b64decode(payload + "==")) | {"email": "evil@example.com"}
    check("payload changed after signing",
          run(keys, "main.example.dev", ".".join([head, b64url(json.dumps(forged).encode()), signature])),
          (False, "bad signature"))
    check("no signature", run(keys, "main.example.dev", head + "." + payload + "."), (False, "bad signature"))
    check("signed by a key Access does not have, under its kid",
          run(keys, "main.example.dev", stranger.token(header={"kid": key.kid})), (False, "bad signature"))
    for name, token in [("garbage", "garbage"), ("two parts", "a.b"), ("not base64", "!!.!!.!!"),
                        ("not json", "e30.bm90IGpzb24.AA"), ("json list", "W10.W10.AA"),
                        ("non-ascii", "é.é.é"), ("huge", "a" * 20000)]:
        allowed, _ = run(keys, "main.example.dev", token)
        check(f"malformed token ({name}) refused", allowed, False)

    # Access rotates its keys: a token under a new kid makes it fetch again,
    # at most once per REFETCH_AFTER seconds.
    keys.clock.now += pa.REFETCH_AFTER + 1
    calls = certs.calls
    check("unknown kid before the rotation", run(keys, "main.example.dev", rotated.token()),
          (False, "unknown signing key"))
    check("unknown kid fetched the certs again", certs.calls, calls + 1)
    certs.keys.append(rotated)
    check("still within REFETCH_AFTER", run(keys, "main.example.dev", rotated.token()),
          (False, "unknown signing key"))
    check("no fetch within REFETCH_AFTER", certs.calls, calls + 1)
    keys.clock.now += pa.REFETCH_AFTER + 1
    check("rotated key passes after REFETCH_AFTER", run(keys, "main.example.dev", rotated.token()),
          (True, "valid token"))

    certs.fail = True
    keys.refresh()
    check("certs endpoint down: the known keys still work", run(keys, "main.example.dev", good),
          (True, "valid token"))
    certs.fail = False
    certs.keys = []
    keys.refresh()
    check("certs without keys: the known keys still work", run(keys, "main.example.dev", good),
          (True, "valid token"))

    keys_down = pa.Keys(ISSUER + "/cdn-cgi/access/certs", fetch=FakeCerts(key), clock=Clock())
    keys_down.fetch.fail = True
    check("certs never fetched: refused", run(keys_down, "main.example.dev", good),
          (False, "unknown signing key"))
    check("certs never fetched: other hosts pass", run(keys_down, "example.com", None),
          (True, "not protected"))
    # The endpoint comes back (cloudflared or the network was down at start):
    # retried after RETRY_AFTER, not after REFETCH_AFTER.
    keys_down.fetch.fail = False
    check("failed fetch: no retry right away", run(keys_down, "main.example.dev", good),
          (False, "unknown signing key"))
    keys_down.clock.now += pa.RETRY_AFTER + 1
    check("failed fetch: retried after RETRY_AFTER", run(keys_down, "main.example.dev", good),
          (True, "valid token"))
    check("refresh() reports success", keys_down.refresh(), True)
    keys_down.fetch.fail = True
    check("refresh() reports failure", keys_down.refresh(), False)


def test_short_key(directory):
    short = Key(directory, "short", bits=1024)
    keys = pa.Keys(ISSUER + "/cdn-cgi/access/certs", fetch=FakeCerts(short), clock=Clock())
    check("a key under 2048 bits is refused", run(keys, "main.example.dev", short.token()),
          (False, "bad signature"))


def test_http(key):
    keys = pa.Keys(ISSUER + "/cdn-cgi/access/certs", fetch=FakeCerts(key), clock=Clock())
    server = pa.serve({"listen": ["127.0.0.1", 0], "issuer": ISSUER + "/",
                       "zones": {"Example.dev": [AUD]}}, keys=keys)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"

    def ask(host, token=None, method="GET"):
        headers = {"X-Forwarded-Host": host}
        if token:
            headers["Cf-Access-Jwt-Assertion"] = token
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method)) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    try:
        check("HTTP: domain preview", ask("pr-1.example.com"), (200, b""))
        check("HTTP: protected, no token", ask("main.example.dev"), (403, pa.DENIED))
        check("HTTP: protected, HEAD", ask("main.example.dev", method="HEAD"), (403, b""))
        # These go by the real clock: no nbf, and exp far ahead or long past.
        check("HTTP: protected, valid token", ask("main.example.dev", key.token(exp=NOW * 2, nbf=None)), (200, b""))
        check("HTTP: protected, expired token",
              ask("main.example.dev", key.token(exp=1_000_000_000))[0], 403)
    finally:
        server.shutdown()
        server.server_close()


def main():
    with tempfile.TemporaryDirectory() as directory:
        key = Key(directory, "kid-1")
        rotated = Key(directory, "kid-2")
        stranger = Key(directory, "stranger")
        test_check(key, rotated, stranger)
        test_short_key(directory)
        test_http(key)
    print("all ok")


if __name__ == "__main__":
    main()

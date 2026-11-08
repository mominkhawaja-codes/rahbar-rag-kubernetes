"""CD smoke test through the Ingress: http://localhost (kind maps port 80 to the node,
where ingress-nginx listens). Exits non-zero, with the reason, on any failure."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE, HOST = "http://localhost", "rahbar.local"


def request(path: str, host: str | None = HOST, body: dict | None = None,
            headers: dict | None = None) -> tuple[int, dict, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET")
    if host:
        req.add_header("Host", host)
    if data:
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = (r.status, dict(r.headers), r.read())
    except urllib.error.HTTPError as e:
        out = (e.code, dict(e.headers), e.read())
    except OSError as e:                     # connection refused, timeout, ...
        out = (0, {}, repr(e).encode())
    LAST.update(status=out[0], body=out[2])
    return out


LAST: dict = {}


def check(cond: bool, message: str) -> None:
    print(("ok   " if cond else "FAIL ") + message)
    if not cond:
        # an annotation is readable from the public API, unlike the job log
        detail = f"{message} | last response: {LAST.get('status')} {LAST.get('body', b'')[:300]!r}"
        print("::error title=ingress smoke test::" + " ".join(detail.replace("%", "%25").split()))
        sys.exit(1)


# The Ingress can take a few seconds to be admitted and synced into nginx.
for _ in range(30):
    status, _, body = request("/")
    if status == 200:
        break
    time.sleep(2)

status, _, _ = request("/", host="not-rahbar.example")
check(status == 404, f"unknown Host is rejected by ingress-nginx (HTTP {status})")

status, headers, body = request("/")
check(status == 200 and b"<title>RAHBAR</title>" in body,
      f"GET / with Host {HOST} serves the UI (HTTP {status}, {len(body)} bytes)")

rid = "cd-smoke-ingress"
status, headers, body = request("/api/ask", headers={"X-Request-ID": rid},
                                body={"question": "When is a student placed on academic warning?"})
check(status == 200, f"POST /api/ask through the Ingress (HTTP {status})")
data = json.loads(body)
check(data.get("request_id") == rid and headers.get("X-Request-ID") == rid,
      "request id echoed in body and header")
check(isinstance(data.get("answered_from_context"), bool) and data["answered_from_context"],
      "answered from context")
retrieved = {(p["source"], p["section"]) for p in data.get("retrieved", [])}
check(bool(retrieved), f"{len(retrieved)} passages retrieved")
cites = [(c["source"], c["section"]) for c in data.get("citations", [])]
check(bool(cites) and all(c in retrieved for c in cites),
      f"every citation is a retrieved passage: {cites}")
print(json.dumps({k: data[k] for k in ("request_id", "answered_from_context", "confidence",
                                       "provider", "citations")}))

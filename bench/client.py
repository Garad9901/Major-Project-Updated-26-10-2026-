"""Minimal client for the running backend, the way the browser talks to it:
CSRF cookie, session login, then POST /api/ask/ and read the SSE stream.

Records what a user experiences: time to first token, total time, the route
the server reports, and the final answer text."""
import json
import time

import requests

BASE = "http://127.0.0.1:8000"
HEADERS = {"X-Forwarded-Proto": "https", "Referer": "https://localhost/", "Origin": "https://localhost"}


class Client:
    def __init__(self, username, password, base=BASE):
        self.base = base
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        # Cookies are Secure (production settings); TLS is terminated by Caddy
        # in deployment, so here the hop is plain HTTP with X-Forwarded-Proto.
        # Let requests send them back over that hop.
        self.s.get(f"{base}/api/auth/csrf/", timeout=30)
        self._unsecure_cookies()
        # Connection: close - otherwise the login's keep-alive socket is reused
        # for the first question, and if the server's 5 s keep-alive expired in
        # between (100 logins take longer than that) the POST meets a reset.
        # That is a harness artifact, not a server failure, so avoid it here.
        r = self.s.post(f"{base}/api/auth/login/", json={"username": username, "password": password},
                        headers={**self._csrf(), "Connection": "close"}, timeout=60)
        self._unsecure_cookies()
        if r.status_code != 200:
            raise RuntimeError(f"login failed for {username}: {r.status_code} {r.text[:200]}")

    def _unsecure_cookies(self):
        for c in self.s.cookies:
            c.secure = False

    def _csrf(self):
        token = self.s.cookies.get("csrftoken", "")
        return {"X-CSRFToken": token}

    def ask(self, question, regenerate=False, conversation=None, timeout=900):
        body = {"question": question}
        if regenerate:
            body["regenerate"] = True
        if conversation:
            body["conversation_id"] = conversation
        t0 = time.perf_counter()
        ttft = None
        meta, answer_parts, final, error, convo = {}, [], None, None, None
        status = None
        try:
            with self.s.post(f"{self.base}/api/ask/", json=body, headers=self._csrf(),
                             stream=True, timeout=timeout) as r:
                status = r.status_code
                if r.status_code != 200:
                    return {"question": question, "status": r.status_code, "error": r.text[:300],
                            "total_ms": (time.perf_counter() - t0) * 1000, "answer": "", "route": None}
                event = None
                for raw in r.iter_lines(decode_unicode=True):
                    if raw is None:
                        continue
                    if raw.startswith("event:"):
                        event = raw[6:].strip()
                    elif raw.startswith("data:"):
                        data = json.loads(raw[5:].strip() or "{}")
                        if event == "token":
                            if ttft is None:
                                ttft = (time.perf_counter() - t0) * 1000
                            answer_parts.append(data.get("text", ""))
                        elif event == "meta":
                            meta = data
                        elif event == "done":
                            final = data.get("answer")
                            if ttft is None:
                                ttft = (time.perf_counter() - t0) * 1000
                        elif event == "error":
                            error = data.get("error")
                        elif event == "conversation":
                            convo = data.get("id")
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"
        self._unsecure_cookies()
        total = (time.perf_counter() - t0) * 1000
        return {"question": question, "status": status, "route": meta.get("route"), "meta": meta,
                "ttft_ms": ttft, "total_ms": total, "answer": final if final is not None else "".join(answer_parts),
                "error": error, "conversation": convo}

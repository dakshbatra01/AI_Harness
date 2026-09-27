"""Dependency-free JSON-over-HTTP with bounded, jittered retries."""
from __future__ import annotations

import http.client
import json
import random
import socket
import time
import urllib.error
import urllib.request

from harness.provider.base import ProviderError
from harness.util import REDACT

RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524, 529}


class HTTPStatusError(ProviderError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {REDACT(body)[:1500]}", status=status, fatal=status in (401, 403))
        self.body = body


def post_json(url: str, payload: dict, headers: dict, timeout: float, max_retries: int, log=None, meta: dict | None = None) -> dict:
    """POST JSON with bounded retries. If `meta` is given, the final response headers are stored in meta["headers"]."""
    data = json.dumps(payload).encode("utf-8")
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        # Some API gateways (e.g. Cloudflare-fronted endpoints) reject the default Python-urllib agent (403/1010).
        if "User-Agent" not in headers:
            req.add_header("User-Agent", "evidence-gated-harness/1.0 (+python-urllib)")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if meta is not None:
                    meta["headers"] = {k.lower(): v for k, v in resp.headers.items()}
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                if attempt < max_retries:
                    attempt += 1
                    if log:
                        log(f"model API returned a non-JSON body; retry {attempt}/{max_retries}")
                    time.sleep(min(30.0, 2.0 * attempt))
                    continue
                raise ProviderError(f"model API returned a non-JSON response: {REDACT(raw[:300].decode('utf-8', 'replace'))}") from None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            finally:
                e.close()
            low = body.lower()
            # Spend caps / billing problems do not recover by retrying.
            # (Match specific phrases: rate-limit messages often link to a ".../billing" upgrade page.)
            non_recoverable = any(s in low for s in ("insufficient_quota", "credit balance is too low",
                                                     "exceeded your current quota", "billing_hard_limit", "billing_not_active"))
            ra = _retry_after(e.headers, cap=None)
            if ra is not None and ra > MAX_RETRY_WAIT_S:  # e.g. a daily quota: waiting will not help this run
                raise HTTPStatusError(e.code, body + f" [retry-after {ra:.0f}s exceeds {MAX_RETRY_WAIT_S}s]") from None
            if e.code in RETRYABLE and attempt < max_retries and not non_recoverable:
                delay = (min(ra, 120.0) if ra is not None else None) or min(60.0, 2.0 * (2 ** attempt)) * (0.5 + random.random())
                if log:
                    log(f"model API HTTP {e.code}; retry {attempt + 1}/{max_retries} in {delay:.1f}s")
                time.sleep(delay)
                attempt += 1
                continue
            raise HTTPStatusError(e.code, body) from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError, http.client.HTTPException, OSError) as e:
            if attempt < max_retries:
                delay = min(60.0, 2.0 * (2 ** attempt)) * (0.5 + random.random())
                if log:
                    log(f"model API network error ({type(e).__name__}); retry {attempt + 1}/{max_retries} in {delay:.1f}s")
                time.sleep(delay)
                attempt += 1
                continue
            raise ProviderError(f"network error contacting model API: {e}") from None


MAX_RETRY_WAIT_S = 300.0


def _retry_after(headers, cap: float | None = 120.0) -> float | None:
    try:
        v = headers.get("retry-after") if headers else None
        if not v:
            return None
        return float(v) if cap is None else min(cap, float(v))
    except (TypeError, ValueError):
        return None

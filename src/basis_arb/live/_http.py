"""Minimal keyless HTTP GET client shared by live/history.py and live/feed.py (read-only public data).

Signed (order) traffic never goes through this module; it lives in live/broker.py, which can only
reach the Binance testnet hosts.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = (418, 429, 500, 502, 503, 504)


class HttpError(RuntimeError):
    """Non-200 response, transport failure or undecodable body (callers must not guess a value)."""

    def __init__(self, url: str, status: int | None, detail: str = "") -> None:
        self.url = url
        self.status = status
        self.detail = detail[:300]
        super().__init__(f"HTTP {status if status is not None else 'ERR'} {url}: {self.detail}")


class PublicClient:
    """requests.Session wrapper: timeout on every call, bounded retries, honors Retry-After on 429/418."""

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (5.0, 15.0),
        retries: int = 2,
        backoff_s: float = 1.0,
        max_retry_after_s: float = 10.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.backoff_s = backoff_s
        self.max_retry_after_s = max_retry_after_s

    def get(self, url: str, params: dict | None = None) -> requests.Response:
        attempt = 0
        while True:
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt < self.retries:
                    attempt += 1
                    time.sleep(self.backoff_s * attempt)
                    continue
                raise HttpError(url, None, f"{type(exc).__name__}: {exc}") from exc
            if resp.status_code == 200:
                return resp
            if resp.status_code in RETRY_STATUSES and attempt < self.retries:
                wait = self.backoff_s * (attempt + 1)
                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None:
                    try:
                        wait = float(retry_after)
                    except ValueError:
                        pass
                if wait <= self.max_retry_after_s:
                    attempt += 1
                    log.warning("HTTP %s from %s; retrying in %.1fs", resp.status_code, url, wait)
                    time.sleep(wait)
                    continue
            raise HttpError(url, resp.status_code, resp.text)

    def get_json(self, url: str, params: dict | None = None) -> Any:
        resp = self.get(url, params)
        try:
            return resp.json()
        except ValueError as exc:
            raise HttpError(url, resp.status_code, "invalid JSON body") from exc

    def get_text(self, url: str, params: dict | None = None) -> str:
        return self.get(url, params).text

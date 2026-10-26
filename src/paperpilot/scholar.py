"""Semantic Scholar 客户端：只取引文关系。

arXiv API 不提供引用信息，引文只有这一条路。只做两件事：谁引了它、它引了谁。
"""

from __future__ import annotations

import time

import httpx

from .ids import base_id

BASE = "https://api.semanticscholar.org/graph/v1"
EDGE_FIELDS = "title,year,citationCount,influentialCitationCount,externalIds"


class ScholarError(RuntimeError):
    """Semantic Scholar 不可用或没有这篇论文。"""


def ext_id(arxiv_id: str) -> str:
    return f"arXiv:{base_id(arxiv_id)}"


class ScholarClient:
    def __init__(self, *, timeout: float = 30.0, retries: int = 3,
                 min_interval: float = 1.1, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.min_interval = max(0.0, float(min_interval))
        self._transport = transport
        self._sleep = sleep
        self._last = 0.0
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout, transport=self._transport, follow_redirects=True,
                headers={"User-Agent": "PaperPilot/0.1 (local research workbench)"})
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()

    def _get(self, path: str, params: dict) -> dict:
        last = ""
        for attempt in range(self.retries):
            self._throttle()
            try:
                response = self._http().get(f"{BASE}{path}", params=params)
                if response.status_code == 429:
                    last = "429 限速"
                    self._sleep(self.min_interval * (attempt + 2))
                    continue
                if response.status_code == 404:
                    raise ScholarError("Semantic Scholar 没有这篇论文（404）")
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
                self._sleep(self.min_interval * (attempt + 1))
        raise ScholarError(last or "Semantic Scholar 请求失败")

    @staticmethod
    def _edges(payload: dict, key: str) -> list[dict]:
        out = []
        for item in payload.get("data") or []:
            node = item.get(key) or {}
            external = node.get("externalIds") or {}
            aid = base_id(str(external.get("ArXiv") or ""))
            out.append({
                "arxiv_id": aid,
                "title": (node.get("title") or "").strip(),
                "year": int(node.get("year") or 0),
                "citations": int(node.get("citationCount") or 0),
                "influential": bool(node.get("influentialCitationCount")),
                "s2_id": node.get("paperId") or "",
            })
        return out

    def references(self, arxiv_id: str, *, limit: int = 40) -> list[dict]:
        payload = self._get(f"/paper/{ext_id(arxiv_id)}/references",
                            {"fields": EDGE_FIELDS, "limit": min(int(limit), 100)})
        return self._edges(payload, "citedPaper")

    def citations(self, arxiv_id: str, *, limit: int = 40) -> list[dict]:
        payload = self._get(f"/paper/{ext_id(arxiv_id)}/citations",
                            {"fields": EDGE_FIELDS, "limit": min(int(limit), 100)})
        return self._edges(payload, "citingPaper")

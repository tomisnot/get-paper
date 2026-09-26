"""Semantic Scholar Graph API 客户端：引文分析的数据源。

arXiv 自己几乎不提供引文数据；S2 的**引用数 / 参考文献 / 被引**是"专项调查"的地基
（docs/SPEC.md 引文分析方向）。典型用法：拿一篇论文的 references 按引用数降序，
高被引且年份早的即"技术起源/奠基"候选，再对它们递归即可往前扒历史。

纪律对齐 infra/arxiv.py：限速 + 429/5xx 指数退避重试 + 磁盘缓存 + 可注入
transport/sleeper（测试不联网）。API key 可选（env SEMANTIC_SCHOLAR_API_KEY / S2_API_KEY），
无 key 也能用（受公共限速）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx

logger = logging.getLogger("paperpilot.scholar")

BASE_URL = "https://api.semanticscholar.org/graph/v1"
USER_AGENT = "paperpilot/0.1 (personal citation analysis)"
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 引文数据变化慢，缓存 7 天

_PAPER_FIELDS = (
    "title,abstract,year,venue,citationCount,referenceCount,"
    "influentialCitationCount,externalIds,authors,tldr,openAccessPdf"
)
# 注意：references/citations 端点的嵌套论文**不支持 tldr**（带上会 400）；tldr 仅 /paper 单篇端点可用。
_EDGE_FIELDS = "title,year,venue,citationCount,influentialCitationCount,externalIds"


class ScholarError(RuntimeError):
    """Semantic Scholar 请求最终失败（或该论文不在 S2）。"""


class _Retryable(RuntimeError):
    """可重试的瞬时错误（429/5xx/网络）。"""


def resolve_api_key(explicit: str | None = None) -> str:
    return (
        explicit
        or os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
        or os.environ.get("S2_API_KEY")
        or ""
    )


def arxiv_ext_id(arxiv_id: str) -> str:
    """把裸 arXiv id 包成 S2 外部 ID；已带前缀（DOI:/CorpusId:/arXiv:）则原样返回。"""
    aid = (arxiv_id or "").strip()
    if not aid:
        return aid
    if ":" in aid:
        return aid
    return f"arXiv:{aid}"


class SemanticScholarClient:
    """带限速、重试、磁盘缓存的 S2 Graph API 客户端。"""

    def __init__(
        self,
        *,
        cache_dir: Path | str | None = None,
        api_key: str | None = None,
        min_interval: float = 1.0,
        timeout: float = 30.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleeper=time.sleep,
    ) -> None:
        self.min_interval = max(0.0, min_interval)
        self.timeout = timeout
        self.retries = max(1, retries)
        self.api_key = resolve_api_key(api_key)
        self.transport = transport
        self._sleep = sleeper
        self._last_request = 0.0
        self._client: httpx.Client | None = None
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---- HTTP ----
    def _http(self) -> httpx.Client:
        if self._client is None:
            headers = {"User-Agent": USER_AGENT}
            if self.api_key:
                headers["x-api-key"] = self.api_key
            self._client = httpx.Client(
                headers=headers, timeout=self.timeout,
                transport=self.transport, follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> SemanticScholarClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 缓存 ----
    def _cache_path(self, key: str) -> Path | None:
        return self.cache_dir / f"{key}.json" if self.cache_dir else None

    def _cache_get(self, key: str):
        path = self._cache_path(key)
        if not path or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def _cache_put(self, key: str, obj) -> None:
        path = self._cache_path(key)
        if path:
            path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    def _rate_limit(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    # ---- 请求 ----
    def _get(self, path: str, params: dict):
        key = hashlib.sha1(
            json.dumps([path, params], sort_keys=True, default=str).encode()
        ).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._rate_limit()
            try:
                resp = self._http().get(f"{BASE_URL}{path}", params=params)
                if resp.status_code == 404:
                    raise ScholarError("Semantic Scholar 里没有这篇论文（404）")
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise _Retryable(f"HTTP {resp.status_code}")
                if resp.status_code >= 400:
                    raise ScholarError(
                        f"Semantic Scholar 拒绝请求（HTTP {resp.status_code}）：fields/参数可能不合法"
                    )
                resp.raise_for_status()
                data = resp.json()
                self._cache_put(key, data)
                return data
            except ScholarError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                backoff = self.min_interval * (2**attempt) + 1
                logger.warning("S2 请求失败（第 %d 次）: %s；%.1fs 后重试", attempt + 1, exc, backoff)
                self._sleep(backoff)
        raise ScholarError(f"Semantic Scholar 请求多次失败: {last_error}")

    # ---- 公开方法 ----
    def paper(self, ext_id: str, fields: str = _PAPER_FIELDS) -> dict:
        """单篇论文度量：citationCount/referenceCount/influentialCitationCount/tldr/year/venue…"""
        return self._get(f"/paper/{ext_id}", {"fields": fields})

    def references(self, ext_id: str, *, limit: int = 50, fields: str = _EDGE_FIELDS) -> list:
        """它引用的论文（边含 intents/isInfluential）——往前追溯起源。单页，limit≤100。"""
        data = self._get(
            f"/paper/{ext_id}/references", {"fields": fields, "limit": min(int(limit), 100)}
        )
        return data.get("data", []) if isinstance(data, dict) else []

    def citations(self, ext_id: str, *, limit: int = 50, fields: str = _EDGE_FIELDS) -> list:
        """引用了它的论文——往后看影响力扩散。单页，limit≤100。"""
        data = self._get(
            f"/paper/{ext_id}/citations", {"fields": fields, "limit": min(int(limit), 100)}
        )
        return data.get("data", []) if isinstance(data, dict) else []

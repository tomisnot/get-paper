"""arXiv 采集器：官方 API（Atom）+ RSS 兜底思路 + 限速/重试/缓存。

合规要点（DESIGN.md §5）：
- 请求间隔默认 3s，429/5xx/超时指数退避重试
- 原始响应本地缓存 24h，重跑当天任务不重复请求 arXiv
- PDF 不预下载（详情页按需）
"""

from __future__ import annotations

import calendar
import hashlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import feedparser
import httpx

logger = logging.getLogger("paperpilot.arxiv")

API_URL = "https://export.arxiv.org/api/query"
USER_AGENT = "paperpilot/0.1 (personal arXiv daily digest)"
CACHE_TTL_SECONDS = 24 * 3600


class ArxivError(RuntimeError):
    """arXiv 请求最终失败。"""


# ---------------------------------------------------------------- 数据结构
@dataclass(frozen=True)
class NormalizedPaper:
    arxiv_id: str
    version: int
    title: str
    abstract: str
    authors: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    primary_category: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    pdf_url: str = ""
    abs_url: str = ""


# ---------------------------------------------------------------- 解析（纯函数，可单测）
def _squash(text: str) -> str:
    return " ".join((text or "").split())


def split_version(entry_id: str) -> tuple[str, int]:
    """"http://arxiv.org/abs/2608.01101v2" → ("2608.01101", 2)；旧式 math.GT/0309136 前缀保留。"""
    raw = (entry_id or "").strip().rstrip("/")
    if "/abs/" in raw:
        raw = raw.split("/abs/", 1)[1]
    if "v" in raw and raw.rsplit("v", 1)[1].isdigit():
        base, _, version = raw.rpartition("v")
        return base, int(version)
    return raw, 1


def _to_datetime(struct) -> datetime | None:
    if not struct:
        return None
    try:
        return datetime.utcfromtimestamp(calendar.timegm(struct))
    except (TypeError, ValueError, OverflowError):
        return None


def entry_to_paper(entry) -> NormalizedPaper:
    raw_id = str(entry.get("id", "")).strip()
    arxiv_id, version = split_version(raw_id)
    title = _squash(entry.get("title", ""))
    abstract = _squash(entry.get("summary", ""))

    authors = [a.get("name", "").strip() for a in entry.get("authors", []) if a.get("name")]
    categories = [t.get("term", "") for t in entry.get("tags", []) if t.get("term")]
    primary = ""
    prim = entry.get("arxiv_primary_category")
    if isinstance(prim, dict):
        primary = prim.get("term", "")
    if not primary and categories:
        primary = categories[0]

    pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    for link in entry.get("links", []):
        if link.get("title") == "pdf" and link.get("href"):
            pdf_url = str(link["href"])
            break

    return NormalizedPaper(
        arxiv_id=arxiv_id,
        version=version,
        title=title,
        abstract=abstract,
        authors=[a for a in authors if a],
        categories=[c for c in categories if c],
        primary_category=primary,
        published_at=_to_datetime(entry.get("published_parsed")),
        updated_at=_to_datetime(entry.get("updated_parsed")),
        pdf_url=pdf_url,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}",
    )


def parse_atom(xml_text: str) -> list[NormalizedPaper]:
    """Atom XML → NormalizedPaper 列表；单条解析失败只跳过该条。"""
    feed = feedparser.parse(xml_text)
    papers: list[NormalizedPaper] = []
    for entry in feed.get("entries", []):
        try:
            papers.append(entry_to_paper(entry))
        except Exception as exc:  # noqa: BLE001
            logger.warning("跳过无法解析的 entry %r: %s", entry.get("id"), exc)
    return papers


def parse_feed(xml_text: str) -> tuple[list[NormalizedPaper], int | None]:
    """parse_atom + 总条数（opensearch:totalResults）。"""
    feed = feedparser.parse(xml_text)
    total_raw = str(feed.feed.get("opensearch_totalresults", ""))
    total = int(total_raw) if total_raw.isdigit() else None
    papers: list[NormalizedPaper] = []
    for entry in feed.get("entries", []):
        try:
            papers.append(entry_to_paper(entry))
        except Exception as exc:  # noqa: BLE001
            logger.warning("跳过无法解析的 entry %r: %s", entry.get("id"), exc)
    return papers, total


# ---------------------------------------------------------------- 查询构造
def _or_cats(categories: Sequence[str]) -> str:
    return " OR ".join(f"cat:{c.strip()}" for c in categories if c.strip())


def _or_keywords(keywords: Sequence[str]) -> str:
    return " OR ".join(f'all:"{k.strip().replace(chr(34), "")}"' for k in keywords if k.strip())


def _date_window(since: datetime | None, until: datetime | None) -> str:
    if not since and not until:
        return ""
    lo = (since or datetime.utcnow()).strftime("%Y%m%d%H%M")
    hi = (until or datetime.utcnow()).strftime("%Y%m%d%H%M")
    return f"submittedDate:[{lo} TO {hi}]"


def build_queries(
    topics: Sequence, categories: Sequence[str] = ()
) -> list[str]:
    """主题 query + 全局分类兜底 query。"""
    queries: list[str] = []
    for topic in topics:
        tq = _or_cats(topic.categories)
        kq = _or_keywords(topic.keywords)
        parts = [f"({tq})" if tq else "", f"({kq})" if kq else ""]
        query = " AND ".join(p for p in parts if p)
        if query and query not in queries:
            queries.append(query)
    all_cats = list(dict.fromkeys([*categories, *[c for t in topics for c in t.categories]]))
    cat_query = _or_cats(all_cats)
    if cat_query:
        wrapped = f"({cat_query})"
        if wrapped not in queries:
            queries.append(wrapped)
    return queries


# ---------------------------------------------------------------- 客户端
class ArxivClient:
    """带限速、重试、磁盘缓存的 arXiv API 客户端。

    transport / sleeper 可注入，便于测试时不发真实请求。
    """

    def __init__(
        self,
        *,
        cache_dir: Path | str | None = None,
        min_interval: float = 3.0,
        timeout: float = 30.0,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        sleeper=time.sleep,
    ) -> None:
        self.min_interval = max(0.0, min_interval)
        self.timeout = timeout
        self.retries = max(1, retries)
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
            self._client = httpx.Client(
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                transport=self.transport,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ArxivClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 缓存 ----
    def _cache_path(self, key: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / f"{key}.xml"

    def _cache_get(self, key: str) -> str | None:
        path = self._cache_path(key)
        if not path or not path.exists():
            return None
        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        return path.read_text(encoding="utf-8")

    def _cache_put(self, key: str, payload: str) -> None:
        path = self._cache_path(key)
        if path:
            path.write_text(payload, encoding="utf-8")

    # ---- 请求 ----
    def _rate_limit(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def query(
        self, search_query: str, *, start: int = 0, max_results: int = 50, sort_by: str = "submittedDate"
    ) -> str:
        params = {
            "search_query": search_query,
            "start": start,
            "max_results": max_results,
            "sortBy": sort_by,
            "sortOrder": "descending",
        }
        key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()
        cached = self._cache_get(key)
        if cached is not None:
            logger.debug("缓存命中: %s", search_query[:60])
            return cached

        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._rate_limit()
            try:
                resp = self._http().get(API_URL, params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise ArxivError(f"HTTP {resp.status_code}")
                resp.raise_for_status()
                self._cache_put(key, resp.text)
                return resp.text
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                backoff = self.min_interval * (2**attempt) + 1
                logger.warning("arXiv 请求失败（第 %d 次）: %s；%.1fs 后重试", attempt + 1, exc, backoff)
                self._sleep(backoff)
        raise ArxivError(f"arXiv 请求多次失败: {last_error}")

    def download_pdf(self, url: str, dest: Path, *, chunk_size: int = 65536) -> int:
        """下载 PDF 到 dest（限速 + 指数退避重试）；返回写入字节数。

        先写 .part 再原子替换，避免半截文件被当成已下载（幂等由调用方按 dest 是否存在判断）。
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._rate_limit()
            tmp = dest.with_suffix(dest.suffix + ".part")
            try:
                with self._http().stream("GET", url) as resp:
                    if resp.status_code == 429 or resp.status_code >= 500:
                        raise ArxivError(f"HTTP {resp.status_code}")
                    resp.raise_for_status()
                    written = 0
                    with open(tmp, "wb") as fh:
                        for block in resp.iter_bytes(chunk_size):
                            fh.write(block)
                            written += len(block)
                tmp.replace(dest)
                return written
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                tmp.unlink(missing_ok=True)
                backoff = self.min_interval * (2**attempt) + 1
                logger.warning("PDF 下载失败（第 %d 次）: %s；%.1fs 后重试", attempt + 1, exc, backoff)
                self._sleep(backoff)
        raise ArxivError(f"PDF 下载多次失败: {last_error}")

    def _fetch_feed(
        self, search_query: str, start: int, max_results: int
    ) -> tuple[list[NormalizedPaper], int | None]:
        xml = self.query(search_query, start=start, max_results=max_results)
        return parse_feed(xml)

    def collect(self, search_query: str, *, max_per_query: int = 50) -> list[NormalizedPaper]:
        """分页收集单个 query 的全部结果（受 max_per_query 限制）。"""
        collected: list[NormalizedPaper] = []
        start = 0
        while True:
            papers, total = self._fetch_feed(search_query, start, self._page_size)
            if not papers:
                break
            collected.extend(papers)
            start += len(papers)
            if total is None or start >= total or start >= max_per_query:
                break
        return collected

    _page_size = 50

    def fetch_candidates(
        self,
        *,
        topics: Sequence = (),
        categories: Sequence[str] = (),
        since: datetime | None = None,
        until: datetime | None = None,
        max_per_query: int = 50,
    ) -> list[NormalizedPaper]:
        """按主题 + 全局分类抓取候选，按 arxiv_id 去重（保留高版本）。"""
        window = _date_window(since, until)
        by_id: dict[str, NormalizedPaper] = {}
        for base_query in build_queries(topics, categories):
            query = f"{base_query} AND {window}" if window else base_query
            try:
                papers = self.collect(query, max_per_query=max_per_query)
            except ArxivError as exc:
                logger.error("query 失败，跳过: %s（%s）", query[:80], exc)
                continue
            for paper in papers:
                existing = by_id.get(paper.arxiv_id)
                if existing is None or paper.version > existing.version:
                    by_id[paper.arxiv_id] = paper
        return list(by_id.values())

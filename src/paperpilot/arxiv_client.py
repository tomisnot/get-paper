"""arXiv 客户端：限速、重试、日历窗、分页到饱和。

只做三件事：把查询式变成论文、把时间范围变成日历、把分页跑到没有下一页。
不相干的事（落库、记账、排序）不在这里。
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import Iterable, Sequence

import httpx

from .ids import base_id, split_id

API = "https://export.arxiv.org/api/query"
USER_AGENT = "PaperPilot/0.1 (local research workbench)"

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


class ArxivError(RuntimeError):
    """arXiv 不可用（限速、网络、解析失败）。"""


def to_api_date(moment: datetime) -> str:
    return moment.strftime("%Y%m%d%H%M")


def date_clause(lo: datetime, hi: datetime) -> str:
    return f"submittedDate:[{to_api_date(lo)} TO {to_api_date(hi)}]"


def day_windows(since: datetime, until: datetime) -> list[tuple[datetime, datetime]]:
    """把区间切成逐日窗口，`to` 含当天整天。"""
    start, end = (since, until) if since <= until else (until, since)
    windows: list[tuple[datetime, datetime]] = []
    cursor = datetime(start.year, start.month, start.day)
    while cursor <= end:
        lo = cursor
        hi = min(cursor + timedelta(days=1) - timedelta(seconds=1), end)
        windows.append((lo, hi))
        cursor += timedelta(days=1)
    return windows


def _or(values: Sequence[str], template: str) -> str:
    parts = [template.format(v) for v in values if v]
    if not parts:
        return ""
    return "(" + " OR ".join(parts) + ")" if len(parts) > 1 else parts[0]


def region_query(regions: Sequence[str], lo: datetime, hi: datetime) -> str:
    """(分类 OR …) AND 这一天。领域全量扫：不掺关键词，一篇不漏。"""
    cats = _or(list(regions or []), "cat:{0}")
    window = date_clause(lo, hi)
    return f"{cats} AND {window}" if cats else window


def phrase_query(phrases: Sequence[str], lo: datetime, hi: datetime) -> str:
    """(短语 OR …) AND 这区间。用于按检索式发现。"""
    terms = _or([f'"{p}"' if " " in p else p for p in (phrases or [])], "all:{0}")
    window = date_clause(lo, hi)
    return f"{terms} AND {window}" if terms else window


def _text(node, path: str, default: str = "") -> str:
    found = node.find(path, NS)
    if found is None or found.text is None:
        return default
    return " ".join(found.text.split())


def parse_entry(entry) -> dict:
    raw_id = _text(entry, "atom:id")
    aid, version = split_id(raw_id)
    authors = [" ".join((a.findtext("atom:name", "", NS) or "").split())
               for a in entry.findall("atom:author", NS)]
    categories = [c.get("term", "") for c in entry.findall("atom:category", NS)]
    primary = ""
    node = entry.find("arxiv:primary_category", NS)
    if node is not None:
        primary = node.get("term", "")
    pdf_url = ""
    for link in entry.findall("atom:link", NS):
        if link.get("title") == "pdf" or link.get("type") == "application/pdf":
            pdf_url = link.get("href", "")
    return {
        "arxiv_id": aid,
        "version": version,
        "title": _text(entry, "atom:title"),
        "abstract": _text(entry, "atom:summary"),
        "authors": [a for a in authors if a],
        "categories": [c for c in categories if c],
        "primary_category": primary or (categories[0] if categories else ""),
        "published_at": _text(entry, "atom:published").replace("T", " ").replace("Z", ""),
        "updated_at": _text(entry, "atom:updated").replace("T", " ").replace("Z", ""),
        "pdf_url": pdf_url,
        "abs_url": f"https://arxiv.org/abs/{aid}" if aid else "",
    }


class ArxivClient:
    def __init__(self, *, min_interval: float = 3.0, timeout: float = 30.0,
                 retries: int = 3, page_size: int = 50,
                 transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.min_interval = max(0.0, float(min_interval))
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.page_size = max(1, int(page_size))
        self._transport = transport
        self._sleep = sleep
        self._last = 0.0
        self._client: httpx.Client | None = None

    # ---------------------------------------------------------------- HTTP
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                transport=self._transport,
                follow_redirects=True)
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

    def raw(self, params: dict) -> ET.Element:
        last_error = ""
        for attempt in range(self.retries):
            self._throttle()
            try:
                response = self._http().get(API, params=params)
                if response.status_code == 429:
                    last_error = "429 限速"
                    self._sleep(self.min_interval * (attempt + 2))
                    continue
                response.raise_for_status()
                return ET.fromstring(response.text)
            except (httpx.HTTPError, ET.ParseError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                self._sleep(self.min_interval * (attempt + 1))
        raise ArxivError(last_error or "arXiv 请求失败")

    # ---------------------------------------------------------------- 查询
    def search(self, query: str, *, start: int = 0, max_results: int = 50) -> dict:
        root = self.raw({"search_query": query, "start": int(start),
                         "max_results": int(max_results),
                         "sortBy": "submittedDate", "sortOrder": "descending"})
        total_text = root.findtext("opensearch:totalResults", "0", NS)
        total = int(total_text) if (total_text or "").isdigit() else 0
        papers = [parse_entry(e) for e in root.findall("atom:entry", NS)]
        return {"papers": [p for p in papers if p["arxiv_id"]], "total": total}

    def fetch_region_window(self, regions: Sequence[str], lo: datetime, hi: datetime,
                            *, max_pages: int = 4) -> tuple[list[dict], dict]:
        """单个自然日 × 一组分类，翻页到饱和。"""
        query = region_query(regions, lo, hi)
        papers: list[dict] = []
        pages = 0
        total = 0
        while pages < max_pages:
            page = self.search(query, start=pages * self.page_size,
                               max_results=self.page_size)
            pages += 1
            total = page["total"]
            papers.extend(page["papers"])
            if len(page["papers"]) < self.page_size:
                break
            if len(papers) >= total:
                break
        saturated = len(papers) >= total and total > 0
        return papers, {"day": lo.date().isoformat(), "pages": pages,
                        "hits": len(papers), "total": total, "saturated": saturated}

    def fetch_regions(self, regions: Sequence[str], since: datetime, until: datetime,
                      *, max_pages_per_day: int = 4,
                      max_requests: int = 120) -> tuple[list[dict], dict]:
        days = day_windows(since, until)
        papers: list[dict] = []
        per_day: list[dict] = []
        requests = 0
        truncated: list[str] = []
        for lo, hi in days:
            if requests >= max_requests:
                truncated.append(lo.date().isoformat())
                continue
            batch, stats = self.fetch_region_window(
                regions, lo, hi, max_pages=min(max_pages_per_day,
                                               max_requests - requests))
            requests += stats["pages"]
            per_day.append(stats)
            papers.extend(batch)
            if not stats["saturated"] and stats["total"] > stats["hits"]:
                truncated.append(stats["day"])
        deduped = _dedupe(papers)
        return deduped, {
            "days": [d.date().isoformat() for d, _ in days],
            "requests": requests,
            "per_day": per_day,
            "saturated_days": [d["day"] for d in per_day if d["saturated"]],
            "truncated_days": truncated,
            "deduped": len(papers) - len(deduped),
        }

    def fetch_phrases(self, phrases: Sequence[str], since: datetime, until: datetime,
                      *, limit: int = 200, max_requests: int = 20) -> tuple[list[dict], dict]:
        """一组短语 × 一个区间，翻页到 limit 或饱和。"""
        query = phrase_query(phrases, since, until)
        papers: list[dict] = []
        pages = 0
        total = 0
        while pages < max_requests and len(papers) < limit:
            page = self.search(query, start=pages * self.page_size,
                               max_results=min(self.page_size, limit - len(papers)))
            pages += 1
            total = page["total"]
            papers.extend(page["papers"])
            if len(page["papers"]) < self.page_size:
                break
        deduped = _dedupe(papers)
        return deduped, {"queries": list(phrases), "requests": pages,
                         "hits": len(papers), "total": total,
                         "deduped": len(papers) - len(deduped)}

    def get(self, arxiv_id: str) -> dict | None:
        aid = base_id(arxiv_id)
        page = self.search(f"id:{aid}", max_results=1)
        return page["papers"][0] if page["papers"] else None


def _dedupe(papers: Iterable[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for paper in papers:
        seen.setdefault(paper["arxiv_id"], paper)
    return list(seen.values())

from __future__ import annotations

import httpx
import pytest

from paperpilot.scholar import ScholarClient, ScholarError, ext_id

REF_PAYLOAD = {"data": [
    {"citedPaper": {"paperId": "s2-1", "title": "First", "year": 2019,
                    "citationCount": 120, "influentialCitationCount": 3,
                    "externalIds": {"ArXiv": "1901.00001"}}},
    {"citedPaper": {"paperId": "s2-2", "title": "No arxiv id", "year": 2020,
                    "citationCount": 5, "influentialCitationCount": 0,
                    "externalIds": {}}},
]}
CITE_PAYLOAD = {"data": [
    {"citingPaper": {"paperId": "s2-3", "title": "Citing", "year": 2026,
                     "citationCount": 2, "influentialCitationCount": 1,
                     "externalIds": {"ArXiv": "2610.25020v2"}}},
]}


def make(handler, **kwargs) -> ScholarClient:
    return ScholarClient(min_interval=0, sleep=lambda _s: None,
                         transport=httpx.MockTransport(handler), **kwargs)


def test_references_parses_edges():
    client = make(lambda request: httpx.Response(200, json=REF_PAYLOAD))
    rows = client.references("2610.25020")
    assert rows[0] == {"arxiv_id": "1901.00001", "title": "First", "year": 2019,
                       "citations": 120, "influential": True, "s2_id": "s2-1"}
    assert rows[1]["arxiv_id"] == ""          # 没有 arXiv 编号的条目照样返回，由调用方丢弃

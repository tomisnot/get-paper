from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from paperpilot.arxiv_client import (ArxivClient, day_windows, parse_entry, phrase_query,
                                     region_query)

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
  <opensearch:totalResults>1</opensearch:totalResults>
  <entry>
    <id>http://arxiv.org/abs/2610.25020v2</id>
    <updated>2026-10-18T05:00:36Z</updated>
    <published>2026-10-18T05:00:36Z</published>
    <title>Fast and High-Fidelity
      Motional-State-Preserving Transport</title>
    <summary>We transport atoms without heating them.</summary>
    <author><name>Wen-hao Wang</name></author>
    <author><name>Jian-gong Hu</name></author>
    <arxiv:primary_category term="physics.atom-ph" />
    <category term="physics.atom-ph" />
    <category term="quant-ph" />
    <link href="http://arxiv.org/abs/2610.25020v2" rel="alternate" />
    <link title="pdf" href="http://arxiv.org/pdf/2610.25020v2" rel="related" />
  </entry>
</feed>
"""


def make_client(handler) -> ArxivClient:
    return ArxivClient(min_interval=0, sleep=lambda _s: None,
                       transport=httpx.MockTransport(handler))


def test_region_query_is_category_times_day():
    query = region_query(["quant-ph", "physics.atom-ph"],
                         datetime(2026, 3, 1), datetime(2026, 3, 1, 23, 59, 59))
    assert "(cat:quant-ph OR cat:physics.atom-ph)" in query
    assert "submittedDate:[202603010000 TO 202603012359]" in query
    assert "all:" not in query


def test_parse_entry_reads_namespaced_fields():
    import xml.etree.ElementTree as ET
    entry = ET.fromstring(ATOM).find("{http://www.w3.org/2005/Atom}entry")
    paper = parse_entry(entry)
    assert paper["arxiv_id"] == "2610.25020"
    assert paper["version"] == "2"
    assert paper["title"].startswith("Fast and High-Fidelity Motional")
    assert paper["authors"] == ["Wen-hao Wang", "Jian-gong Hu"]
    assert paper["primary_category"] == "physics.atom-ph"
    assert paper["categories"] == ["physics.atom-ph", "quant-ph"]
    assert paper["pdf_url"].endswith("2610.25020v2")
    assert paper["published_at"] == "2026-10-18 05:00:36"


def test_search_paginates_until_short_page():
    calls = []

    def handler(request):
        calls.append(dict(httpx.QueryParams(request.url.query)))
        start = int(dict(httpx.QueryParams(request.url.query))["start"])
        body = ATOM if start == 0 else ATOM.replace("<opensearch:totalResults>1<", "<opensearch:totalResults>0<").replace(
            "<entry>", "<entry-x>").replace("</entry>", "</entry-x>").replace("<entry-x>", "<none>").replace("</entry-x>", "</none>")
        return httpx.Response(200, text=body)

    client = make_client(handler)
    page = client.search("cat:quant-ph", max_results=1)
    assert page["total"] == 1
    assert len(page["papers"]) == 1
    assert calls[0]["search_query"] == "cat:quant-ph"

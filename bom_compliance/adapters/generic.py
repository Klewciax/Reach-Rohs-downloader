"""Generyczny mechanizm awaryjny.

Dla producentów bez dedykowanego adaptera (lub gdy adapter nic nie znalazł):
  * odpytuje skonfigurowane wyszukiwarki na stronie producenta (search_urls),
  * przechodzi (BFS, ograniczona głębokość i liczba stron) po stronach compliance
    producenta oraz linkach, które wyglądają na związane z RoHS/REACH/środowiskiem,
  * zbiera linki do dokumentów wyłącznie z oficjalnych domen producenta.
Jeśli brak skonfigurowanych stron compliance, startuje od strony głównej domeny.
"""
from __future__ import annotations

import logging
import re
from collections import deque

from ..http_client import host_allowed
from ..models import SearchResult
from .base import AdapterContext, BaseAdapter

log = logging.getLogger(__name__)

_FOLLOW_RE = re.compile(
    r"rohs|reach|svhc|environment|compliance|sustainab|material|quality|certificat|declaration|green|eco",
    re.IGNORECASE,
)


def crawl_compliance_pages(mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
    m = ctx.manufacturer
    helper = BaseAdapter()
    seeds: list[str] = [t.format(mpn=BaseAdapter.q(mpn)) for t in m.search_urls]
    seeds += m.compliance_pages
    if not seeds:
        seeds = [f"https://www.{m.domains[0]}/"]
        result.notes.append("Brak skonfigurowanych stron compliance – przeszukiwanie od strony głównej producenta")
    max_pages = ctx.settings.generic_max_pages
    max_depth = ctx.settings.generic_max_depth
    queue = deque((s, 0) for s in seeds)
    seen: set[str] = set()
    visited = 0
    before = len(result.candidates)
    while queue and visited < max_pages:
        url, depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        page = helper.fetch_html(url, ctx, result)
        visited += 1
        if page is None:
            continue
        soup, final_url = page
        helper.collect_document_links(soup, final_url, mpn, ctx, result)
        helper.detect_request_form(soup, final_url, result)
        if depth + 1 > max_depth:
            continue
        for link, text in helper.iter_links(soup, final_url):
            if link in seen or not host_allowed(link, m.domains):
                continue
            if link.lower().split("?")[0].endswith((".pdf", ".xls", ".xlsx", ".zip", ".xml")):
                continue
            if _FOLLOW_RE.search(link) or _FOLLOW_RE.search(text):
                queue.append((link, depth + 1))
    log.info("Generic[%s] %s: odwiedzono %d stron, znaleziono %d kandydatów",
             m.key, mpn, visited, len(result.candidates) - before)


class GenericAdapter(BaseAdapter):
    key = "generic"

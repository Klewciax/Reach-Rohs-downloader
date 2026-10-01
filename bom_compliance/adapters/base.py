"""Wspólny interfejs adapterów producentów."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin, urldefrag

from bs4 import BeautifulSoup

from ..classify import DOC_EXTENSIONS, looks_like_login_page, scope_from_link, types_from_link
from ..config import Settings
from ..http_client import DomainNotAllowed, FetchError, LoginRequired, PoliteSession, RobotsDisallowed, host_allowed
from ..models import Candidate, DocType, ManufacturerInfo, Scope, SearchResult

log = logging.getLogger(__name__)


@dataclass
class AdapterContext:
    session: PoliteSession
    settings: Settings
    manufacturer: ManufacturerInfo


class BaseAdapter:
    """Klasa bazowa. Adapter producenta nadpisuje `find_part_documents`.

    Kolejność działania `search()`:
      1. find_part_documents()   – API / stały wzorzec URL / strona produktu (specyficzne)
      2. general_documents z YAML – ogólne oświadczenia producenta
      3. fallback generyczny     – crawl stron compliance, jeśli (1) nic nie dał
    """

    key = "base"
    #: czy uruchamiać generyczny crawl, gdy adapter nie znalazł dokumentów dla MPN
    generic_fallback = True

    def search(self, mpn: str, ctx: AdapterContext) -> SearchResult:
        result = SearchResult()
        try:
            self.find_part_documents(mpn, ctx, result)
        except Exception as exc:  # błąd adaptera nie może zatrzymać całego przebiegu
            log.exception("Adapter %s: błąd dla %s", self.key, mpn)
            result.errors.append(f"Błąd adaptera {self.key}: {exc}")
        has_specific = any(c.scope != Scope.GENERAL for c in result.candidates)
        if not has_specific and self.generic_fallback:
            from .generic import crawl_compliance_pages

            crawl_compliance_pages(mpn, ctx, result)
        if ctx.settings.download_general_statements:
            for doc in ctx.manufacturer.general_documents:
                types = {DocType(t) for t in doc.get("types", [])}
                result.add(Candidate(url=doc["url"], doc_types=types, scope=Scope(doc.get("scope", "general")),
                                     title=doc.get("title", ""), note="ogólne oświadczenie producenta"))
        for page in ctx.manufacturer.compliance_pages:
            if page not in result.manual_urls:
                result.manual_urls.append(page)
        return result

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        """Do nadpisania: dodaj kandydatów (result.add) dla konkretnego MPN."""

    # ------------------------------------------------- cykl życia / longevity
    @staticmethod
    def base_part(mpn: str) -> str:
        """Numer produktu bez sufiksów obudowy/opakowania (np. ATMEGA328P-AU -> ATMEGA328P)."""
        return re.split(r"[-/#,. ]", mpn.strip())[0] or mpn.strip()

    def product_page_urls(self, mpn: str, ctx: AdapterContext) -> list[str]:
        """Strony produktu, z których odczytywany jest status cyklu życia.

        Domyślnie z szablonów `product_pages` w manufacturers.yaml; obsługiwane pola:
        {mpn}, {mpn_lower}, {base}, {base_lower}. Adapter może nadpisać metodę.
        """
        base = self.base_part(mpn)
        out = []
        for tpl in ctx.manufacturer.product_pages:
            url = tpl.format(mpn=self.q(mpn), mpn_lower=self.q(mpn.lower()),
                             base=self.q(base), base_lower=self.q(base.lower()))
            if url not in out:
                out.append(url)
        return out

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def q(mpn: str) -> str:
        return quote(mpn.strip(), safe="")

    def fetch_html(self, url: str, ctx: AdapterContext, result: SearchResult,
                   not_found_ok: bool = True) -> tuple[BeautifulSoup, str] | None:
        """Pobiera stronę HTML producenta, obsługując typowe błędy i rejestrując je w wyniku.

        Wynik (również negatywny) jest cache'owany w obrębie przebiegu, więc strony
        compliance wspólne dla wielu MPN są pobierane tylko raz.
        """
        cache = ctx.session.page_cache
        if url not in cache:
            cache[url] = self._fetch_html_uncached(url, ctx)
        kind, a, b = cache[url]
        if kind == "ok":
            return BeautifulSoup(a, "html.parser"), b
        if kind == "login":
            result.login_required.append(a)
        elif kind == "robots":
            result.robots_blocked.append(a)
        elif kind == "note" and not (a.startswith("Brak strony (404)") and not not_found_ok):
            result.notes.append(a)
        elif kind == "error":
            result.errors.append(a)
        return None

    @staticmethod
    def _fetch_html_uncached(url: str, ctx: AdapterContext) -> tuple[str, str, str]:
        try:
            resp = ctx.session.get(url, ctx.manufacturer.domains)
        except LoginRequired as exc:
            return "login", exc.url, ""
        except RobotsDisallowed as exc:
            return "robots", exc.url, ""
        except DomainNotAllowed as exc:
            return "note", f"Przekierowanie poza domenę producenta zablokowane: {exc.url}", ""
        except FetchError as exc:
            if exc.status == 404:
                return "note", f"Brak strony (404): {url}", ""
            if exc.status == 403:
                return "error", f"Odmowa dostępu (403) – możliwa ochrona anty-bot: {url}", ""
            return "error", str(exc), ""
        ctype = resp.headers.get("Content-Type", "")
        if "html" not in ctype.lower():
            resp.close()
            return "nonhtml", "", ""
        text = resp.text
        final = resp.url or url
        if looks_like_login_page(text):
            return "login", final, ""
        return "ok", text, final

    @staticmethod
    def iter_links(soup: BeautifulSoup, base_url: str):
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "javascript:", "tel:", "#")):
                continue
            url = urldefrag(urljoin(base_url, href))[0]
            yield url, " ".join(a.get_text(" ").split())

    def collect_document_links(self, soup: BeautifulSoup, page_url: str, mpn: str, ctx: AdapterContext,
                               result: SearchResult, url_must_contain: str | None = None,
                               default_types: set[DocType] | None = None) -> int:
        """Dodaje linki do dokumentów RoHS/REACH/MCD z danej strony. Zwraca liczbę dodanych."""
        added = 0
        for url, text in self.iter_links(soup, page_url):
            if not host_allowed(url, ctx.manufacturer.domains):
                continue
            low = url.lower()
            if url_must_contain and url_must_contain.lower() not in low:
                continue
            types = types_from_link(url, text) or (default_types or set())
            if not types:
                continue
            is_doc = low.split("?")[0].endswith(DOC_EXTENSIONS) or any(
                k in low for k in ("/docs/", "/download", "/dgdl/", "/resource/", "/lit/", "/media/", "/doc/")
            )
            if not is_doc:
                continue
            result.add(Candidate(url=url, doc_types=types, scope=scope_from_link(mpn, url, text),
                                 title=text, source_page=page_url))
            added += 1
        return added

    @staticmethod
    def detect_request_form(soup: BeautifulSoup, page_url: str, result: SearchResult) -> None:
        """Jeśli strona zawiera formularz prośby o dokument (pole e-mail), zapisz to."""
        keywords = ("rohs", "reach", "compliance", "certificate", "declaration", "environment")
        for form in soup.find_all("form"):
            has_email = form.find("input", attrs={"type": "email"}) or form.find(
                "input", attrs={"name": lambda n: n and "mail" in n.lower()})
            # Pomijamy np. formularze newslettera: formularz musi dotyczyć zgodności/dokumentów.
            form_text = (form.get_text(" ") + " " + str(form.get("action", ""))).lower()
            if has_email and any(k in form_text for k in keywords):
                if page_url not in result.form_required:
                    result.form_required.append(page_url)
                return

"""Wyszukiwanie kontaktu do działu compliance na OFICJALNEJ stronie producenta.

Adresy e-mail są raportowane wyłącznie, jeśli zostały faktycznie znalezione na
pobranej stronie z domeny producenta (i same należą do tej domeny). Niczego nie
zgadujemy – brak potwierdzenia = "do ręcznej weryfikacji".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from .adapters.base import AdapterContext, BaseAdapter
from .http_client import host_allowed
from .models import ManufacturerInfo, SearchResult

log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RELEVANT = re.compile(r"rohs|reach|environment|compliance|quality|sustainab|material|product.?stewardship|eco",
                       re.IGNORECASE)
_CONTACT_LINK = re.compile(r"contact|support|request|inquiry|enquiry|kontakt", re.IGNORECASE)


@dataclass
class ContactInfo:
    emails: list[tuple[str, str]] = field(default_factory=list)  # (email, strona źródłowa)
    pages_verified: list[str] = field(default_factory=list)
    pages_unverified: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)
    extra_links: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.emails:
            parts.append("E-mail (znaleziony na stronie producenta): " +
                         "; ".join(f"{e} [źródło: {src}]" for e, src in self.emails))
        if self.forms:
            parts.append("Formularz: " + "; ".join(self.forms))
        if self.pages_verified:
            parts.append("Strony kontaktowe/compliance (zweryfikowane HTTP 200): " + "; ".join(self.pages_verified))
        if self.extra_links:
            parts.append("Powiązane linki kontaktowe ze strony producenta: " + "; ".join(self.extra_links[:5]))
        if self.pages_unverified:
            parts.append("DO RĘCZNEJ WERYFIKACJI (strona nieosiągalna z narzędzia): " + "; ".join(self.pages_unverified))
        if not parts:
            parts.append("DO RĘCZNEJ WERYFIKACJI: nie znaleziono kontaktu na oficjalnej stronie producenta")
        return "\n".join(parts)


def _emails_in(soup: BeautifulSoup, page_url: str, domains: list[str]) -> list[tuple[str, str]]:
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().startswith("mailto:"):
            found.append(href[7:].split("?")[0].strip())
    found += EMAIL_RE.findall(soup.get_text(" "))
    out = []
    for e in dict.fromkeys(x.strip().strip(".").lower() for x in found):
        if host_allowed(e.split("@")[-1], domains):
            out.append((e, page_url))
    return out


def find_contacts(manufacturer: ManufacturerInfo, ctx: AdapterContext) -> ContactInfo:
    info = ContactInfo()
    helper = BaseAdapter()
    pages = list(dict.fromkeys(manufacturer.contact_pages + manufacturer.compliance_pages))
    if not pages and manufacturer.domains:  # np. producent wykryty automatycznie – strona główna
        pages = [f"https://www.{manufacturer.domains[0]}/"]
    for url in pages:
        scratch = SearchResult()
        page = helper.fetch_html(url, ctx, scratch)
        if page is None:
            if scratch.login_required:
                info.forms.append(f"{url} (wymaga logowania)")
                info.pages_verified.append(url)
            else:
                info.pages_unverified.append(url)
            continue
        soup, final = page
        info.pages_verified.append(final)
        for e in _emails_in(soup, final, manufacturer.domains):
            if e[0] not in (x[0] for x in info.emails):
                info.emails.append(e)
        helper.detect_request_form(soup, final, scratch)
        info.forms.extend(f for f in scratch.form_required if f not in info.forms)
        for link, text in helper.iter_links(soup, final):
            if (host_allowed(link, manufacturer.domains) and _CONTACT_LINK.search(link + " " + text)
                    and _RELEVANT.search(link + " " + text) and link not in info.extra_links):
                info.extra_links.append(link)
    return info

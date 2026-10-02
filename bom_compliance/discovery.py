"""Automatyczne wykrywanie oficjalnej strony producenta spoza rejestru.

Kandydaci na domenę (w kolejności wiarygodności):
  1. strona producenta podana przez Nexar/Octopart (manufacturer.homepageUrl),
  2. domena karty katalogowej z API dystrybutorów (jeśli nie jest domeną dystrybutora/hostingu),
  3. domeny utworzone z nazwy producenta (np. "Acme Connectors Ltd" -> acmeconnectors.com, acme.com, …).

Każdy kandydat jest WERYFIKOWANY: pobieramy stronę główną (z poszanowaniem robots.txt)
i sprawdzamy, czy tytuł / nazwa witryny / początek treści zawiera nazwę producenta.
Niezweryfikowana domena nie jest używana. Wykryci producenci są zapisywani do pliku
(discovered_manufacturers.yaml), więc kolejne uruchomienia korzystają z wyniku, a raport
zawsze oznacza ich jako "wykryty automatycznie – zweryfikuj".
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from .http_client import FetchError, PoliteSession
from .manufacturers import normalize_name, strip_legal
from .models import ManufacturerInfo

log = logging.getLogger(__name__)

# Domeny, które NIGDY nie są stroną producenta (dystrybutorzy, agregatory, hosting, wyszukiwarki)
NOT_MANUFACTURER = {
    "digikey.com", "mouser.com", "octopart.com", "nexar.com", "tme.eu", "tme.com", "farnell.com", "newark.com",
    "element14.com", "arrow.com", "avnet.com", "rs-online.com", "rsdelivers.com", "lcsc.com", "alldatasheet.com",
    "datasheetspdf.com", "datasheet4u.com", "datasheetarchive.com", "findchips.com", "alibaba.com", "aliexpress.com",
    "amazon.com", "amazonaws.com", "cloudfront.net", "azureedge.net", "akamaihd.net", "google.com", "github.com",
    "ebay.com", "conrad.com", "conrad.de", "reichelt.de", "botland.com.pl", "kamami.pl", "allegro.pl", "tinyurl.com",
    "componentsearchengine.com", "snapeda.com", "ultralibrarian.com", "z2data.com", "siliconexpert.com",
}
_GENERIC_TOKENS = {"ELECTRONICS", "ELECTRONIC", "TECHNOLOGY", "TECHNOLOGIES", "COMPONENTS", "SEMICONDUCTOR",
                   "SEMICONDUCTORS", "INDUSTRIES", "SYSTEMS", "MANUFACTURING", "CORP", "AND", "OF", "THE"}
TLDS = (".com", ".de", ".eu", ".co.jp", ".com.tw", ".co.uk", ".pl", ".ch", ".fr", ".it", ".cn")


@dataclass
class Discovery:
    info: ManufacturerInfo
    method: str
    evidence: str
    pages: dict = None  # url -> (kind, html, final_url) – strony już pobrane (do cache przebiegu)


def _tokens(name: str) -> list[str]:
    return [t for t in strip_legal(normalize_name(name)).split() if t]


def _significant(name: str) -> list[str]:
    toks = _tokens(name)
    sig = [t for t in toks if t not in _GENERIC_TOKENS and len(t) >= 2]
    return sig or toks


def guess_domains(name: str) -> list[str]:
    toks = [t.lower() for t in _tokens(name)]
    sig = [t.lower() for t in _significant(name)]
    if not toks:
        return []
    stems = list(dict.fromkeys(["".join(toks), "".join(sig), sig[0] if sig else toks[0], "-".join(toks)]))
    stems = [s for s in stems if len(s) >= 3]
    out = []
    for tld in TLDS[:3]:
        for st in stems:
            out.append(st + tld)
    for tld in TLDS[3:]:
        out.append(stems[0] + tld)
    return list(dict.fromkeys(out))


def name_matches_site(name: str, html: str) -> tuple[bool, str]:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ") if soup.title else ""
    site = " ".join(m.get("content", "") for m in soup.find_all("meta")
                    if m.get("property") in ("og:site_name", "og:title") or m.get("name") in ("application-name",
                                                                                              "author", "copyright"))
    head_text = " ".join(soup.get_text(" ").split())[:4000]
    sig = _significant(name)
    for label, hay in (("tytuł strony", title), ("nazwa witryny", site), ("treść strony", head_text)):
        hay_n = " " + normalize_name(hay) + " "
        hay_c = hay_n.replace(" ", "")
        if sig and all(f" {t} " in hay_n or (len(t) >= 5 and t in hay_c) for t in sig):
            return True, f"{label}: '{(hay if label != 'treść strony' else title or hay)[:120].strip()}'"
    return False, ""


class ManufacturerDiscovery:
    MAX_GUESSES = 10

    def __init__(self, session: PoliteSession, hub, cache_file: str | Path | None, enabled: bool = True):
        # Osobna sesja bez ponowień: zgadywane domeny często nie istnieją – nie czekamy na backoff.
        from dataclasses import replace

        self.session = PoliteSession(replace(session.settings, max_retries=0), sleep=session._sleep)
        self.hub = hub
        self.enabled = enabled
        self.cache_file = Path(cache_file) if cache_file else None
        self._results: dict[str, Discovery | None] = {}
        self._pages: dict[str, tuple[str, str, str]] = {}
        self.new_entries: dict[str, dict] = {}

    def _verify(self, domain: str, name: str) -> str | None:
        if domain in NOT_MANUFACTURER or any(domain.endswith("." + d) for d in NOT_MANUFACTURER):
            return None
        for url in (f"https://www.{domain}/", f"https://{domain}/"):
            try:
                resp = self.session.get(url, [domain])
            except FetchError as exc:
                log.debug("Discovery %s: %s", url, exc)
                continue
            if "html" not in resp.headers.get("Content-Type", "").lower():
                continue
            ok, why = name_matches_site(name, resp.text)
            if ok:
                self._pages[url] = ("ok", resp.text, resp.url or url)
                return f"{resp.url or url} ({why})"
        return None

    def discover(self, raw_name: str, mpn: str) -> Discovery | None:
        key = strip_legal(normalize_name(raw_name))
        if not self.enabled or not key:
            return None
        if key in self._results:
            return self._results[key]
        from .classify import compact
        from .distributors import registrable_domain

        candidates: list[tuple[str, str]] = []
        canonical = raw_name.strip()
        if self.hub is not None and self.hub.active and mpn:
            for part in self.hub.lookup(mpn):
                if compact(part.mpn) != compact(mpn) or not _same_company(raw_name, part.manufacturer):
                    continue
                canonical = part.manufacturer or canonical
                if part.manufacturer_homepage:
                    candidates.append((registrable_domain(part.manufacturer_homepage),
                                       f"strona producenta wg {part.source}"))
                if part.datasheet_url:
                    candidates.append((registrable_domain(part.datasheet_url),
                                       f"domena karty katalogowej wg {part.source}"))
        candidates += [(d, "domena utworzona z nazwy producenta")
                       for d in guess_domains(raw_name)[:self.MAX_GUESSES]]
        tried = set()
        result = None
        for domain, method in candidates:
            if not domain or domain in tried:
                continue
            tried.add(domain)
            evidence = self._verify(domain, raw_name) or (self._verify(domain, canonical) if canonical != raw_name
                                                          else None)
            if evidence:
                slug = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
                info = ManufacturerInfo(key=f"auto_{slug}", name=canonical, domains=[domain], adapter="generic",
                                        aliases=list(dict.fromkeys([raw_name.strip(), canonical])))
                result = Discovery(info, method, evidence, dict(self._pages))
                self.new_entries[info.key] = {
                    "name": info.name, "adapter": "generic", "domains": [domain], "aliases": info.aliases,
                    "auto_discovered": {"method": method, "evidence": evidence,
                                        "date": datetime.now(timezone.utc).date().isoformat(),
                                        "verify": "wykryto automatycznie – zweryfikuj domenę"},
                }
                log.warning("Wykryto stronę producenta '%s': %s (%s)", raw_name, domain, method)
                break
        self._results[key] = result
        return result

    def save(self, extra_path: Path | None = None) -> None:
        """Dopisuje nowo wykrytych producentów do pliku cache (i kopii w katalogu wyników)."""
        if not self.new_entries:
            return
        for path in [p for p in (self.cache_file, extra_path) if p]:
            data = {}
            if path.is_file():
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            mans = data.setdefault("manufacturers", {})
            mans.update(self.new_entries)
            header = ("# Producenci wykryci automatycznie przez bom-compliance (domena zweryfikowana nazwą na stronie\n"
                      "# głównej). Plik jest wczytywany przy kolejnych uruchomieniach. Możesz go przejrzeć lub\n"
                      "# przenieść wpisy do manufacturers.yaml.\n")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _same_company(a: str, b: str) -> bool:
    """Czy dwie nazwy producenta oznaczają tę samą firmę (po normalizacji)."""
    na, nb = strip_legal(normalize_name(a)), strip_legal(normalize_name(b))
    if not na or not nb:
        return False
    if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
        return True
    sa, sb = set(_significant(a)), set(_significant(b))
    return bool(sa) and (sa <= set(nb.split()) or sb <= set(na.split()))

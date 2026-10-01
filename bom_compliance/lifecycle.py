"""Opcjonalne sprawdzenie statusu cyklu życia (Active / NRND / LTB / EOL) i longevity.

Zasady (te same co dla RoHS/REACH):
  * dane wyłącznie ze stron na oficjalnych domenach producenta,
  * każda informacja ma URL źródłowy, datę sprawdzenia i fragment tekstu (dowód),
  * status przypisany do konkretnego MPN tylko wtedy, gdy etykieta stoi przy tym MPN;
    w przeciwnym razie jest oznaczony jako status strony produktu / rodziny ("page"),
  * niczego nie zgadujemy – brak danych = UNKNOWN / "do ręcznej weryfikacji".
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from .adapters import AdapterContext, BaseAdapter
from .classify import _mpn_regex, compact, is_exact_end, mpn_prefixes
from .downloader import Downloader, NotADocument, safe_name
from .http_client import FetchError
from .models import BomItem, Candidate, DocType, LifecycleInfo, LifecycleStatus, LongevityInfo, Scope, SearchResult

log = logging.getLogger(__name__)

# Kolejność ma znaczenie: bardziej specyficzne frazy przed ogólnymi
# ("not recommended for new designs" zawiera "recommended for new designs").
_STATUS_PHRASES: list[tuple[LifecycleStatus, re.Pattern]] = [
    (LifecycleStatus.NRND, re.compile(r"not\s+recommended\s+for\s+new\s+designs?|\bNRND\b|not\s+for\s+new\s+designs?", re.I)),
    (LifecycleStatus.LAST_TIME_BUY, re.compile(r"last[\s-]+time[\s-]+buy|\bLIFEBUY\b|life[\s-]?time\s+buy|\bLTB\b|last\s+shipments?", re.I)),
    (LifecycleStatus.OBSOLETE, re.compile(r"\bobsolete\b|\bdiscontinued\b|end[\s-]+of[\s-]+life|\bEOL\b|\binactive\b|no\s+longer\s+(?:manufactured|available)", re.I)),
    (LifecycleStatus.MATURE, re.compile(r"\bmature\b", re.I)),
    (LifecycleStatus.PREVIEW, re.compile(r"\bpreview\b|\bproposal\b|\bsampling\b|pre[\s-]?production|advance\s+information|coming\s+soon", re.I)),
    (LifecycleStatus.ACTIVE, re.compile(r"\bactive\b|in\s+production|\bproduction\b|recommended\s+for\s+new\s+designs?|\breleased\b|\bpreferred\b", re.I)),
]

# Etykieta "Status: ..." / "Lifecycle: ..." / "Marketing Status ..." itp.
_LABEL_RE = re.compile(
    r"(?:product\s+|part\s+|marketing\s+|production\s+)?(?:status|life\s*-?\s*cycle(?:\s+status)?)\s*[:\-–]?\s*"
    r"(?P<v>[A-Za-z][A-Za-z /\-]{2,45})",
    re.I,
)

_YEARS_RE = re.compile(r"\b(\d{1,2})\s*[- ]?\s*(?:\+\s*)?years?\b", re.I)
_END_RE = re.compile(r"(?:until|through|till|thru|end(?:\s+date)?|ends?|longevity\s+end|commitment\s+end|available\s+until)"
                     r"\D{0,20}?((?:19|20)\d\d)", re.I)
_START_RE = re.compile(r"(?:from|since|start(?:ing)?(?:\s+date)?|launch(?:ed|\s+date)?|commitment\s+start)\D{0,20}?((?:19|20)\d\d)",
                       re.I)
_ANY_YEAR = re.compile(r"\b((?:19|20)\d\d)\b")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _earliest_status(text: str) -> tuple[LifecycleStatus, str]:
    """Status, którego fraza występuje najwcześniej (przy remisie – bardziej specyficzny)."""
    best: tuple[int, int, LifecycleStatus, str] | None = None
    for prio, (status, rx) in enumerate(_STATUS_PHRASES):
        m = rx.search(text)
        if m and (best is None or (m.start(), prio) < (best[0], best[1])):
            best = (m.start(), prio, status, m.group(0))
    # "Not recommended for new designs" zawiera "recommended for new designs" – NRND zaczyna się wcześniej.
    return (best[2], best[3]) if best else (LifecycleStatus.UNKNOWN, "")


def classify_status(label: str) -> LifecycleStatus:
    return _earliest_status(label)[0]


def _mpn_positions(text: str, mpn: str) -> list[int]:
    """Pozycje samodzielnych wystąpień MPN (separatory - / . , # spacja traktowane elastycznie)."""
    rx = _mpn_regex(mpn)
    if rx is None:
        return []
    return [m.start() for m in rx.finditer(text) if is_exact_end(text, m.end())]


def parse_lifecycle(text: str, mpn: str) -> tuple[LifecycleStatus, str, str, str]:
    """Zwraca (status, etykieta, zakres 'part'/'page', fragment-dowód) z tekstu strony."""
    text = _norm_ws(text)
    # 1) Status stojący przy dokładnym MPN (np. tabela wariantów zamówieniowych).
    base = re.split(r"[^A-Z0-9]", mpn.upper())[0][:6]
    for pos in _mpn_positions(text, mpn):
        after = text[pos:pos + 140]
        # Okno kończy się na następnym numerze z tej samej rodziny (kolejny wiersz tabeli wariantów).
        skip = len(compact(mpn)) + 2
        nxt = re.search(r"(?<![A-Za-z0-9])" + re.escape(base), after[skip:], re.I) if len(base) >= 3 else None
        window = after[skip - 2: skip + nxt.start()] if nxt else after[skip - 2:]
        st, label = _earliest_status(window)
        if st != LifecycleStatus.UNKNOWN:
            return st, label, "part", text[max(0, pos - 40):pos + 160]
    # 2) Etykieta "Status: ..." na stronie (status produktu / rodziny).
    for m in _LABEL_RE.finditer(text):
        st, label = _earliest_status(m.group("v"))
        if st != LifecycleStatus.UNKNOWN:
            return st, label, "page", text[max(0, m.start() - 60):m.end() + 60]
    return LifecycleStatus.UNKNOWN, "", "", ""


def parse_longevity(text: str, mpn: str, require_keyword: bool = False) -> LongevityInfo | None:
    """Szuka MPN (lub prefiksu rodziny) na liście / w dokumencie longevity i wyciąga okres."""
    text = _norm_ws(text)
    scope, positions = "part", _mpn_positions(text, mpn)
    if not positions:
        for p in mpn_prefixes(mpn, min_len=5):
            positions = [m.start() for m in re.finditer(r"(?<![A-Za-z0-9])" + re.escape(p), text, re.I)]
            if positions:
                scope = "family"
                break
    if not positions:
        return None
    for pos in positions:
        window = text[max(0, pos - 120):pos + 300]
        if require_keyword and not re.search(r"longevity|years?|availability|supply", window, re.I):
            continue
        info = LongevityInfo(found=True, scope=scope, evidence=window)
        years = [int(y) for y in _YEARS_RE.findall(window) if 3 <= int(y) <= 30]
        info.years = years[0] if years else None
        end = _END_RE.search(window)
        start = _START_RE.search(window)
        if end:
            info.end_year, info.end_basis = int(end.group(1)), "explicit"
        if start:
            info.start_year = int(start.group(1))
        if info.end_year is None:
            all_years = sorted({int(y) for y in _ANY_YEAR.findall(window)})
            if info.start_year is None and len(all_years) >= 2:
                info.start_year, info.end_year, info.end_basis = all_years[0], all_years[-1], \
                    "explicit (heurystyka: najwcześniejszy/najpóźniejszy rok w wierszu – zweryfikuj)"
            elif info.years and (info.start_year or len(all_years) == 1):
                info.start_year = info.start_year or all_years[0]
                info.end_year, info.end_basis = info.start_year + info.years, "start+years"
        return info
    return None


class LifecycleChecker:
    def __init__(self, ctx_factory, downloader: Downloader, out_dir: Path, settings):
        self.ctx_factory = ctx_factory
        self.downloader = downloader
        self.docs_dir = out_dir / "documents"
        self.settings = settings

    # ---------------------------------------------------------------- status
    def lifecycle(self, item: BomItem, adapter: BaseAdapter) -> LifecycleInfo:
        ctx: AdapterContext = self.ctx_factory(item.manufacturer)
        urls = adapter.product_page_urls(item.mpn, ctx)
        if not urls:
            return LifecycleInfo(note="Brak skonfigurowanej strony produktu dla tego producenta – do ręcznej weryfikacji",
                                 checked_at=_now())
        best: LifecycleInfo | None = None
        problems = []
        for url in urls:
            scratch = SearchResult()
            page = adapter.fetch_html(url, ctx, scratch)
            if page is None:
                problems += scratch.errors + scratch.notes + [f"wymaga logowania: {u}" for u in scratch.login_required] \
                    + [f"robots.txt: {u}" for u in scratch.robots_blocked]
                continue
            soup, final = page
            text = soup.get_text(" ")
            status, label, scope, evidence = parse_lifecycle(text, item.mpn)
            if status == LifecycleStatus.UNKNOWN:
                problems.append(f"nie znaleziono etykiety statusu na {final}")
                continue
            info = LifecycleInfo(status=status, label=label, scope=scope, source_url=final,
                                 evidence=evidence[:400], checked_at=_now())
            if scope == "page":
                info.note = "status odczytany ze strony produktu/rodziny – nie z wiersza konkretnego MPN; zweryfikuj"
            if self.settings.save_lifecycle_snapshots:
                info.snapshot = str(self._snapshot(item, ctx, url, final))
            if scope == "part":
                return info
            best = best or info
        if best:
            return best
        return LifecycleInfo(note="; ".join(dict.fromkeys(problems)) or "brak danych", checked_at=_now())

    def _snapshot(self, item: BomItem, ctx: AdapterContext, url: str, final: str) -> Path:
        kind, html, _ = ctx.session.page_cache.get(url, ("", "", ""))
        folder = self.docs_dir / safe_name(item.manufacturer_name) / safe_name(item.mpn)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = folder / f"{safe_name(item.mpn)}__{safe_name(item.manufacturer_name)}__LIFECYCLE__{stamp}.html"
        header = f"<!-- source: {final} | retrieved (UTC): {_now()} -->\n"
        path.write_text(header + (html if kind == "ok" else ""), encoding="utf-8")
        return path

    # ------------------------------------------------------------- longevity
    def longevity(self, item: BomItem, adapter: BaseAdapter) -> LongevityInfo:
        m = item.manufacturer
        ctx: AdapterContext = self.ctx_factory(m)
        general_sources: list[str] = []
        # 1) strona produktu (np. "Longevity commitment: 10 years")
        for url in adapter.product_page_urls(item.mpn, ctx):
            page = adapter.fetch_html(url, ctx, SearchResult())
            if page:
                soup, final = page
                text = soup.get_text(" ")
                kw = next((k for k in re.finditer(r"longevity[^.]{0,160}", _norm_ws(text), re.I)
                           if _YEARS_RE.search(k.group(0)) or _ANY_YEAR.search(k.group(0))), None)
                if kw and _mpn_positions(text, item.mpn):
                    info = parse_longevity(item.mpn + " " + kw.group(0), item.mpn) or LongevityInfo(found=True)
                    scope = "part" if compact(item.mpn) in compact(url) else "family"
                    info.found, info.scope, info.source_url = True, scope, final
                    info.program, info.evidence = "deklaracja na stronie produktu", kw.group(0)
                    return self._finish(info, item, ctx)
        # 2) listy programów longevity (HTML)
        for url in m.longevity_pages:
            scratch = SearchResult()
            page = adapter.fetch_html(url, ctx, scratch)
            if page is None:
                continue
            soup, final = page
            info = self._from_table(soup, item.mpn) or parse_longevity(soup.get_text(" "), item.mpn)
            if info:
                info.source_url, info.program = final, info.program or "program longevity producenta"
                return self._finish(info, item, ctx)
            general_sources.append(final)
        # 3) dokumenty longevity / polityki EOL (PDF) – pobierane, przeszukiwane pod kątem MPN
        docs = []
        if self.settings.download_longevity_documents:
            for d in m.longevity_documents:
                cand = Candidate(url=d["url"], doc_types={DocType.LONGEVITY}, scope=Scope(d.get("scope", "general")),
                                 title=d.get("title", ""), fixed_types=True, note="dokument longevity / polityka EOL")
                try:
                    doc = self.downloader.download(cand, item)
                except (FetchError, NotADocument) as exc:
                    log.info("Longevity: nie pobrano %s: %s", d["url"], exc)
                    continue
                docs.append(doc)
                info = parse_longevity(self.downloader.text_for(d["url"]), item.mpn, require_keyword=True)
                if info:
                    info.source_url, info.program, info.docs = d["url"], d.get("title", ""), [doc]
                    return self._finish(info, item, ctx)
        info = LongevityInfo(found=False, scope="general", docs=docs)
        sources = general_sources + [d.url for d in docs]
        if sources:
            info.source_url = "\n".join(sources)
            info.note = ("MPN nie występuje na listach longevity producenta; dostępne są tylko ogólne zasady "
                         "(polityka EOL / longevity) – brak deklaracji 'do kiedy' dla MPN")
        else:
            info.note = "Brak zweryfikowanych źródeł longevity dla tego producenta – do ręcznej weryfikacji"
        return info

    @staticmethod
    def _from_table(soup: BeautifulSoup, mpn: str) -> LongevityInfo | None:
        """Wiersze tabel (np. lista NXP / ST) – tekst wiersza z nagłówkami kolumn jako kontekstem."""
        for table in soup.find_all("table"):
            headers = [_norm_ws(th.get_text(" ")) for th in table.find_all("th")]
            for tr in table.find_all("tr"):
                cells = [_norm_ws(td.get_text(" ")) for td in tr.find_all("td")]
                if not cells:
                    continue
                row = " | ".join(cells)
                labelled = " | ".join(f"{h}: {c}" for h, c in zip(headers, cells)) if len(headers) == len(cells) else row
                info = parse_longevity(row, mpn)
                if info:
                    info.evidence = labelled
                    # Kolumny nazwane wprost (Longevity / End / Start) mają pierwszeństwo nad heurystyką.
                    for h, c in zip(headers, cells):
                        hl = h.lower()
                        year = _ANY_YEAR.search(c)
                        num = re.match(r"^\s*(\d{1,2})\b", c)
                        if re.search(r"end|until|through|expir", hl) and year:
                            info.end_year, info.end_basis = int(year.group(1)), "explicit (kolumna tabeli)"
                        elif re.search(r"start|launch|from|since", hl) and year:
                            info.start_year = int(year.group(1))
                        elif re.search(r"longevity|years|period|commitment", hl) and num and 3 <= int(num.group(1)) <= 30:
                            info.years = int(num.group(1))
                    if info.end_year is None and info.start_year and info.years:
                        info.end_year, info.end_basis = info.start_year + info.years, "start+years"
                    return info
        return None

    def _finish(self, info: LongevityInfo, item: BomItem, ctx: AdapterContext) -> LongevityInfo:
        info.evidence = (info.evidence or "")[:500]
        if info.scope == "family":
            info.note = (info.note + "; " if info.note else "") + \
                "dopasowanie po prefiksie rodziny – zweryfikuj, czy obejmuje dokładny MPN"
        if info.end_year is None:
            info.note = (info.note + "; " if info.note else "") + "nie udało się ustalić daty końcowej – zweryfikuj w źródle"
        return info

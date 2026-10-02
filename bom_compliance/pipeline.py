"""Główna logika: pozycje BoM -> adaptery -> pobieranie -> statusy."""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .adapters import AdapterContext, get_adapter
from .config import Settings
from .downloader import Downloader, NotADocument
from .classify import compact, types_from_link
from .discovery import ManufacturerDiscovery, _same_company
from .distributors import DOC_KEYWORDS, DistributorHub
from .lifecycle import LifecycleChecker
from .manufacturers import ManufacturerRegistry
from .mpn import FORM_FULL, MpnCheck, inspect_mpn, packaging_trims, wildcard_prefix
from .http_client import DomainNotAllowed, FetchError, LoginRequired, PoliteSession, RobotsDisallowed, host_allowed
from .models import (BomItem, Candidate, DocType, ItemResult, LifecycleInfo, LongevityInfo, ManufacturerInfo, Scope,
                     SearchResult, Status)

log = logging.getLogger(__name__)

_SCOPE_ORDER = {Scope.PART: 0, Scope.FAMILY: 1, Scope.GENERAL: 2}


def is_success(status: Status, settings: Settings) -> bool:
    if status == Status.DOWNLOADED:
        return True
    if status == Status.DOWNLOADED_FAMILY:
        return settings.count_family_as_success
    if status == Status.GENERAL_ONLY:
        return settings.count_general_as_success
    return False


class Pipeline:
    def __init__(self, settings: Settings, out_dir: Path, session: PoliteSession | None = None,
                 registry: ManufacturerRegistry | None = None):
        self.settings = settings
        self.session = session or PoliteSession(settings)
        self.registry = registry
        self.downloader = Downloader(self.session, settings, out_dir)
        self.lifecycle = LifecycleChecker(lambda m: AdapterContext(self.session, settings, m),
                                          self.downloader, out_dir, settings)
        self.hub = DistributorHub(self.session, settings.distributor_sources if settings.distributor_fallback
                                  or settings.auto_discover_manufacturers else [], settings.api_keys)
        self.discovery = ManufacturerDiscovery(self.session, self.hub, settings.discovered_manufacturers_file,
                                               settings.auto_discover_manufacturers)

    def process(self, items: list[BomItem], progress=None) -> list[ItemResult]:
        results = []
        for n, item in enumerate(items, 1):
            log.info("[%d/%d] %s / %s", n, len(items), item.manufacturer_name, item.mpn)
            res = self.process_item(item)
            results.append(res)
            if progress:
                progress(n, len(items), res)
        return results

    def process_item(self, item: BomItem) -> ItemResult:
        res = ItemResult(item=item)
        if item.manufacturer is None:
            self._discover(item, res)
        if item.manufacturer is None:
            if self.settings.distributor_fallback and self.hub.active:
                return self._distributor_only(item, res)
            res.rohs_status = res.reach_status = Status.UNKNOWN_MANUFACTURER
            res.reasons.append(
                f"Producent '{item.manufacturer_raw}' spoza rejestru – nie udało się automatycznie wykryć i "
                "zweryfikować jego strony" + ("" if self.hub.active else
                                              " (brak kluczy API dystrybutorów, które pomagają w wykryciu)") +
                ". Nic nie pobrano – do ręcznej weryfikacji")
            if self.settings.check_lifecycle:
                res.lifecycle = LifecycleInfo(note="nieznany producent – do ręcznej weryfikacji")
            if self.settings.check_longevity:
                res.longevity = LongevityInfo(note="nieznany producent – do ręcznej weryfikacji")
            return res
        if item.match_method == "fuzzy":
            res.notes.append(f"Producent dopasowany w przybliżeniu: '{item.manufacturer_raw}' -> "
                             f"{item.manufacturer.name} – zweryfikuj")

        ctx = AdapterContext(self.session, self.settings, item.manufacturer)
        adapter = get_adapter(item.manufacturer.adapter)
        self._inspect_mpn(item, adapter, ctx, res)
        search = adapter.search(item.mpn, ctx)
        self._download_candidates(item, search, res)
        if not self._has_specific(res):
            self._fallback_trimmed(item, adapter, ctx, search, res)
        if self.settings.distributor_fallback and self.hub.active and (
                not self._covers_both(res) or self.settings.distributor_lookup_always):
            self._distributor_fallback(item, res, search)

        res.manual_urls = list(dict.fromkeys(search.manual_urls))
        res.notes.extend(search.notes)
        res.rohs_status = self._status_for(DocType.ROHS, res, search)
        res.reach_status = self._status_for(DocType.REACH, res, search)
        res.reasons = self._reasons(res, search)
        self._lifecycle_and_longevity(item, adapter, res)
        return res

    def _inspect_mpn(self, item: BomItem, adapter, ctx: AdapterContext, res: ItemResult) -> None:
        """Skrót / wzorzec / pełny numer – sprawdzenie na stronie producenta i ewentualne rozwinięcie."""
        if item.wildcard:
            item.mpn = wildcard_prefix(item.mpn_bom or item.mpn) or item.mpn
        if not self.settings.inspect_mpn:
            return
        try:
            check = inspect_mpn(item, adapter, ctx, self.settings.expand_abbreviated_mpn)
        except Exception as exc:  # analiza MPN nie może zatrzymać przebiegu
            log.exception("Analiza MPN: błąd dla %s", item.mpn)
            check = MpnCheck(notes=[f"błąd analizy MPN: {exc}"])
        item.mpn_check = check
        if check.expanded_to:
            item.mpn = check.expanded_to
        if item.hints and not check.expanded_to and check.form != FORM_FULL:
            res.notes.append("W innych kolumnach BoM (np. opis) występuje dłuższy numer: " + ", ".join(item.hints)
                             + " – możliwe, że to pełny MPN")

    # ----------------------------------------------- producent spoza rejestru
    def _discover(self, item: BomItem, res: ItemResult) -> None:
        found = self.discovery.discover(item.manufacturer_raw, item.mpn)
        if not found:
            return
        if self.registry is not None:
            self.registry.add(found.info)
        for url, entry in (found.pages or {}).items():
            self.session.page_cache.setdefault(url, entry)
        item.manufacturer, item.match_method = found.info, "auto"
        res.notes.append(f"Producent spoza rejestru – oficjalna strona wykryta automatycznie: {found.info.domains[0]} "
                         f"({found.method}; potwierdzenie: {found.evidence}) – zweryfikuj")

    def _distributor_only(self, item: BomItem, res: ItemResult) -> ItemResult:
        """Producent nieznany i bez wykrytej strony – próbujemy tylko dystrybutorów (para MPN + nazwa z BoM)."""
        item.manufacturer = ManufacturerInfo(key="?" + item.manufacturer_raw, name=item.manufacturer_raw,
                                             domains=[], adapter="generic", aliases=[item.manufacturer_raw])
        search = SearchResult()
        self._distributor_fallback(item, res, search)
        res.rohs_status = self._status_for(DocType.ROHS, res, search)
        res.reach_status = self._status_for(DocType.REACH, res, search)
        for attr in ("rohs_status", "reach_status"):
            if getattr(res, attr) in (Status.NOT_FOUND, Status.ERROR):
                setattr(res, attr, Status.UNKNOWN_MANUFACTURER)
        res.reasons = [f"Producent '{item.manufacturer_raw}' spoza rejestru, jego strony nie udało się wykryć – "
                       "sprawdzono tylko dystrybutorów"] + self._reasons(res, search)
        if self.settings.check_lifecycle:
            res.lifecycle = LifecycleInfo(note="nieznany producent – status wg dystrybutorów w kolumnie obok")
        if self.settings.check_longevity:
            res.longevity = LongevityInfo(note="nieznany producent – do ręcznej weryfikacji")
        return res

    # ------------------------------------------------------- dystrybutorzy
    def _part_matches(self, part, item: BomItem) -> bool:
        if compact(part.mpn) != compact(item.mpn):
            return False  # tylko dokładny MPN
        if self.registry is not None and item.manufacturer and not item.manufacturer.key.startswith(("?", "auto_")):
            m, _ = self.registry.resolve(part.manufacturer)
            if m is not None:
                return m.key == item.manufacturer.key
        names = [item.manufacturer_raw] + ([item.manufacturer.name, *item.manufacturer.aliases]
                                           if item.manufacturer else [])
        return any(_same_company(n, part.manufacturer) for n in names if n)

    def _distributor_fallback(self, item: BomItem, res: ItemResult, search: SearchResult) -> None:
        parts = [p for p in self.hub.lookup(item.mpn) if self._part_matches(p, item)]
        res.distributor_parts = parts
        if not parts:
            if self.hub.active:
                res.notes.append("Dystrybutorzy: brak oferty z dokładnym MPN tego producenta")
            return
        if self._covers_both(res):
            return  # tylko statusy (distributor_lookup_always)
        cands = []
        for p in parts:
            for d in p.documents:
                label = f"{d.title} {d.url}"
                if not DOC_KEYWORDS.search(label):
                    continue
                if re.search(r"datasheet|data sheet", d.title, re.I) and not re.search(r"rohs|reach|environ|compl",
                                                                                       label, re.I):
                    continue
                cands.append(Candidate(url=d.url, doc_types=types_from_link(d.url, d.title) or
                                       {DocType.ROHS, DocType.REACH}, scope=Scope.PART, title=d.title,
                                       source=p.source, extra_domains=self.hub.doc_domains(p.source),
                                       note=f"źródło: {p.source}"))
        if cands:
            extra = SearchResult(candidates=cands)
            self._download_candidates(item, extra, res)
            search.login_required += extra.login_required
            search.notes += [f"[dystrybutor] {n}" for n in extra.notes]
        else:
            res.notes.append("Dystrybutorzy: oferta znaleziona, ale bez dokumentów RoHS/REACH (tylko statusy)")

    def _covers_both(self, res: ItemResult) -> bool:
        def ok(t):
            return any(t in d.doc_types and d.scope == Scope.PART for d in res.docs)
        return ok(DocType.ROHS) and ok(DocType.REACH)

    def _has_specific(self, res: ItemResult) -> bool:
        return any(d.scope != Scope.GENERAL and d.doc_types & {DocType.ROHS, DocType.REACH} for d in res.docs)

    def _fallback_trimmed(self, item: BomItem, adapter, ctx: AdapterContext, search: SearchResult,
                          res: ItemResult) -> None:
        """Gdy pełny numer (z sufiksem opakowania) nic nie dał – szukaj formy bez sufiksu.
        Znalezione dokumenty są oznaczane jako zbiorcze, bo nie zawierają pełnego MPN."""
        known = {c.url for c in search.candidates}
        for trimmed, why in packaging_trims(item.mpn):
            extra = adapter.search(trimmed, ctx)
            new = [c for c in extra.candidates if c.scope != Scope.GENERAL and c.url not in known]
            known |= {c.url for c in extra.candidates}
            if not new:
                continue
            res.notes.append(f"Brak dokumentów dla pełnego MPN – szukano także formy {trimmed} ({why})")
            extra.candidates = new
            self._download_candidates(item, extra, res)
            search.login_required += extra.login_required
            search.form_required += extra.form_required
            search.notes += extra.notes
            if self._has_specific(res):
                return

    def _lifecycle_and_longevity(self, item: BomItem, adapter, res: ItemResult) -> None:
        if self.settings.check_lifecycle:
            try:
                res.lifecycle = self.lifecycle.lifecycle(item, adapter)
            except Exception as exc:  # opcja dodatkowa nie może przerwać przebiegu
                log.exception("Lifecycle: błąd dla %s", item.mpn)
                res.lifecycle = LifecycleInfo(note=f"błąd: {exc}")
        if self.settings.check_longevity:
            try:
                res.longevity = self.lifecycle.longevity(item, adapter)
            except Exception as exc:
                log.exception("Longevity: błąd dla %s", item.mpn)
                res.longevity = LongevityInfo(note=f"błąd: {exc}")

    def _download_candidates(self, item: BomItem, search: SearchResult, res: ItemResult) -> None:
        cands = sorted(search.candidates, key=lambda c: _SCOPE_ORDER[c.scope])
        attempted = 0
        for cand in cands:
            if attempted >= self.settings.max_docs_per_item:
                res.notes.append(f"Osiągnięto limit {self.settings.max_docs_per_item} dokumentów na pozycję")
                break
            if not host_allowed(cand.url, list(item.manufacturer.domains) + list(cand.extra_domains)):
                log.warning("Pomijam URL spoza domen producenta / zaufanego dystrybutora: %s", cand.url)
                continue
            if cand.scope != Scope.PART and self._covered(res, cand):
                continue  # już mamy lepszy (specyficzny) dokument tego rodzaju
            attempted += 1
            try:
                doc = self.downloader.download(cand, item)
            except LoginRequired as exc:
                search.login_required.append(exc.url)
            except RobotsDisallowed as exc:
                search.robots_blocked.append(exc.url)
            except DomainNotAllowed as exc:
                search.notes.append(f"Odrzucono przekierowanie poza domenę producenta: {exc.url}")
            except NotADocument as exc:
                search.notes.append(str(exc))
            except FetchError as exc:
                if exc.status == 404:
                    search.notes.append(f"Brak dokumentu pod adresem (404): {cand.url}")
                else:
                    search.errors.append(str(exc))
            else:
                if any(d.sha256 == doc.sha256 for d in res.docs):
                    continue  # identyczna treść pod innym URL
                res.docs.append(doc)

    @staticmethod
    def _covered(res: ItemResult, cand: Candidate) -> bool:
        wanted = cand.doc_types & {DocType.ROHS, DocType.REACH} or {DocType.ROHS, DocType.REACH}
        best = {t: min((_SCOPE_ORDER[d.scope] for d in res.docs if t in d.doc_types), default=9) for t in wanted}
        return all(v <= _SCOPE_ORDER[cand.scope] for v in best.values())

    @staticmethod
    def _status_for(t: DocType, res: ItemResult, search: SearchResult) -> Status:
        scopes = {d.scope for d in res.docs if t in d.doc_types}
        if Scope.PART in scopes:
            return Status.DOWNLOADED
        if Scope.FAMILY in scopes:
            return Status.DOWNLOADED_FAMILY
        if search.login_required:
            return Status.LOGIN_REQUIRED
        if search.form_required:
            return Status.FORM_REQUIRED
        if Scope.GENERAL in scopes:
            return Status.GENERAL_ONLY
        if search.robots_blocked:
            return Status.BLOCKED_ROBOTS
        if search.errors:
            return Status.ERROR
        return Status.NOT_FOUND

    def _reasons(self, res: ItemResult, search: SearchResult) -> list[str]:
        out: list[str] = []
        statuses = {res.rohs_status, res.reach_status}
        if all(is_success(s, self.settings) for s in statuses) and not (statuses & {Status.DOWNLOADED_FAMILY}):
            return out
        if Status.DOWNLOADED_FAMILY in statuses:
            out.append("Dokument zbiorczy dla rodziny/serii (nie dla konkretnego MPN)")
        if search.login_required:
            out.append("Wymaga logowania: " + ", ".join(dict.fromkeys(search.login_required)))
        if search.form_required:
            out.append("Dostępne przez formularz: " + ", ".join(dict.fromkeys(search.form_required)))
        if Status.GENERAL_ONLY in statuses:
            out.append("Znaleziono tylko ogólne oświadczenie producenta (bez MPN)")
        if search.robots_blocked:
            out.append("Zablokowane przez robots.txt: " + ", ".join(dict.fromkeys(search.robots_blocked)))
        if search.errors:
            out.append("Błędy: " + " | ".join(dict.fromkeys(search.errors)))
        if Status.NOT_FOUND in statuses and not search.candidates:
            out.append("Nie znaleziono dokumentu na stronie producenta")
        elif Status.NOT_FOUND in statuses:
            missing = [t.value for t, s in ((DocType.ROHS, res.rohs_status), (DocType.REACH, res.reach_status))
                       if s == Status.NOT_FOUND]
            out.append(f"Brak dokumentu {'/'.join(missing)} – sprawdzone źródła nie zawierały go")
        return out

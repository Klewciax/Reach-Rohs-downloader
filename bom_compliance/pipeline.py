"""Główna logika: pozycje BoM -> adaptery -> pobieranie -> statusy."""
from __future__ import annotations

import logging
from pathlib import Path

from .adapters import AdapterContext, get_adapter
from .config import Settings
from .downloader import Downloader, NotADocument
from .lifecycle import LifecycleChecker
from .http_client import DomainNotAllowed, FetchError, LoginRequired, PoliteSession, RobotsDisallowed, host_allowed
from .models import (BomItem, Candidate, DocType, ItemResult, LifecycleInfo, LongevityInfo, Scope, SearchResult,
                     Status)

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
    def __init__(self, settings: Settings, out_dir: Path, session: PoliteSession | None = None):
        self.settings = settings
        self.session = session or PoliteSession(settings)
        self.downloader = Downloader(self.session, settings, out_dir)
        self.lifecycle = LifecycleChecker(lambda m: AdapterContext(self.session, settings, m),
                                          self.downloader, out_dir, settings)

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
            res.rohs_status = res.reach_status = Status.UNKNOWN_MANUFACTURER
            res.reasons.append(
                f"Producent '{item.manufacturer_raw}' nie występuje w rejestrze (config/manufacturers.yaml) – "
                "brak zweryfikowanej domeny, nic nie pobrano. Do ręcznej weryfikacji / dodaj producenta do rejestru")
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
        search = adapter.search(item.mpn, ctx)
        self._download_candidates(item, search, res)

        res.manual_urls = list(dict.fromkeys(search.manual_urls))
        res.notes.extend(search.notes)
        res.rohs_status = self._status_for(DocType.ROHS, res, search)
        res.reach_status = self._status_for(DocType.REACH, res, search)
        res.reasons = self._reasons(res, search)
        self._lifecycle_and_longevity(item, adapter, res)
        return res

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
            if not host_allowed(cand.url, item.manufacturer.domains):
                log.warning("Pomijam URL spoza domen producenta: %s", cand.url)
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

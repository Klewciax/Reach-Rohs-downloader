"""Pobieranie, weryfikacja i zapis dokumentów (z metadanymi źródła)."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .classify import extract_text, looks_like_login_page, scope_from_text, sniff_kind, types_from_text
from .config import Settings
from .http_client import FetchError, LoginRequired, PoliteSession
from .models import BomItem, Candidate, DocType, DownloadedDoc, Scope

log = logging.getLogger(__name__)

ACCEPTED_KINDS = {"pdf", "xml", "xls", "xlsx", "zip", "docx"}


class NotADocument(Exception):
    """Odpowiedź nie jest dokumentem (np. strona błędu HTML zamiast PDF)."""


def safe_name(s: str, max_len: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9._+-]+", "_", s.strip()).strip("._")
    return (s or "x")[:max_len]


def _types_label(types: set[DocType]) -> str:
    order = [DocType.ROHS, DocType.REACH, DocType.MCD, DocType.LONGEVITY]
    return "-".join(t.value for t in order if t in types) or "DOC"


class _Fetched:
    """Surowa treść pobrana spod URL (cache w obrębie przebiegu)."""

    def __init__(self, url: str, final_url: str, data: bytes, kind: str, content_type: str, when: str, text: str):
        self.url, self.final_url, self.data, self.kind = url, final_url, data, kind
        self.content_type, self.when, self.text = content_type, when, text
        self.sha256 = hashlib.sha256(data).hexdigest()
        self.saved_path: Path | None = None


class Downloader:
    def __init__(self, session: PoliteSession, settings: Settings, out_dir: Path):
        self.session = session
        self.settings = settings
        self.docs_dir = out_dir / "documents"
        self._cache: dict[str, _Fetched | Exception] = {}

    def _fetch(self, cand: Candidate, domains: list[str]) -> _Fetched:
        cached = self._cache.get(cand.url)
        if isinstance(cached, Exception):
            raise cached
        if cached is not None:
            return cached
        try:
            fetched = self._fetch_uncached(cand, domains)
        except (FetchError, NotADocument) as exc:
            self._cache[cand.url] = exc
            raise
        self._cache[cand.url] = fetched
        return fetched

    def _fetch_uncached(self, cand: Candidate, domains: list[str]) -> _Fetched:
        limit = int(self.settings.max_download_mb * 1024 * 1024)
        resp = self.session.get(cand.url, domains, stream=True)
        try:
            chunks, size = [], 0
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > limit:
                    raise NotADocument(f"Plik większy niż {self.settings.max_download_mb} MB: {cand.url}")
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            resp.close()
        when = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        ctype = resp.headers.get("Content-Type", "")
        kind = sniff_kind(data, resp.url or cand.url, ctype)
        if kind == "html":
            html = data.decode("utf-8", errors="ignore")
            if looks_like_login_page(html):
                raise LoginRequired(resp.url or cand.url, "Dokument dostępny po zalogowaniu")
            if not cand.allow_html:
                raise NotADocument(f"Zamiast dokumentu zwrócono stronę HTML: {cand.url}")
        elif kind not in ACCEPTED_KINDS:
            raise NotADocument(f"Nierozpoznany format odpowiedzi ({ctype or 'brak Content-Type'}): {cand.url}")
        if len(data) < 200:
            raise NotADocument(f"Pusta lub zbyt mała odpowiedź ({len(data)} B): {cand.url}")
        text = extract_text(data, kind, self.settings.pdf_text_pages)
        return _Fetched(cand.url, resp.url or cand.url, data, kind, ctype, when, text)

    def download(self, cand: Candidate, item: BomItem) -> DownloadedDoc:
        """Pobiera kandydata, klasyfikuje go względem pozycji BoM i zapisuje na dysk."""
        assert item.manufacturer is not None
        fetched = self._fetch(cand, item.manufacturer.domains)
        notes = [cand.note] if cand.note else []

        text = fetched.text
        if len(text.strip()) >= 40:  # mniej = prawdopodobnie skan bez warstwy tekstowej
            detected = set() if cand.fixed_types else types_from_text(text)
            types = detected or set(cand.doc_types)
            if not detected and not cand.fixed_types:
                notes.append("rodzaj dokumentu nie potwierdzony w treści – przyjęto z kontekstu linku")
            scope, why = scope_from_text(item.mpn, text)
            mpn_verified = "yes" if scope == Scope.PART else "no"
            if scope != Scope.PART and cand.scope == Scope.PART:
                notes.append(f"źródło wskazywało dokument części, ale {why} – do ręcznej weryfikacji")
            elif scope == Scope.FAMILY:
                notes.append(f"dokument zbiorczy: {why}")
        else:
            types, scope, mpn_verified = set(cand.doc_types), cand.scope, "unknown"
            notes.append("nie udało się odczytać tekstu (skan / format) – rodzaj i zakres wg źródła, do ręcznej weryfikacji")

        check = item.mpn_check
        abbreviated = check is not None and getattr(check, "form", "").startswith("skrócony") \
            and not getattr(check, "expanded_to", "")
        if scope == Scope.PART and (item.wildcard or abbreviated):
            scope, mpn_verified = Scope.FAMILY, "no"
            notes.append("MPN w BoM jest skrótem/wzorcem – dokument nie potwierdza konkretnego wariantu zamówieniowego")

        path = self._save(fetched, item, types, scope)
        return DownloadedDoc(
            url=cand.url, final_url=fetched.final_url, path=str(path), sha256=fetched.sha256,
            downloaded_at=fetched.when, content_type=fetched.kind, doc_types=types, scope=scope,
            mpn_verified=mpn_verified, title=cand.title, note="; ".join(notes),
            shared=scope != Scope.PART,
        )

    def _save(self, fetched: _Fetched, item: BomItem, types: set[DocType], scope: Scope) -> Path:
        if fetched.saved_path is not None:
            return fetched.saved_path  # ten sam plik już zapisany (np. dokument zbiorczy)
        mfr = safe_name(item.manufacturer_name)
        ext = fetched.kind if fetched.kind != "bin" else "dat"
        if scope == Scope.PART:
            folder = self.docs_dir / mfr / safe_name(item.mpn)
            name = f"{safe_name(item.mpn)}__{mfr}__{_types_label(types)}__{fetched.sha256[:8]}.{ext}"
        else:
            folder = self.docs_dir / mfr / "_shared"
            orig = safe_name(Path(unquote(urlsplit(fetched.final_url).path)).stem or "document", 50)
            name = f"{mfr}__{_types_label(types)}__{scope.value}__{orig}__{fetched.sha256[:8]}.{ext}"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(fetched.data)
        meta = {
            "source_url": fetched.url,
            "final_url": fetched.final_url,
            "downloaded_at_utc": fetched.when,
            "sha256": fetched.sha256,
            "format": fetched.kind,
            "http_content_type": fetched.content_type,
            "manufacturer": item.manufacturer_name,
            "first_requested_for_mpn": item.mpn,
            "doc_types": sorted(t.value for t in types),
            "scope": scope.value,
        }
        path.with_name(path.name + ".source.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                                                              encoding="utf-8")
        fetched.saved_path = path
        log.info("Zapisano %s (%s)", path, fetched.url)
        return path

    def text_for(self, url: str) -> str:
        """Tekst wyodrębniony z wcześniej pobranego dokumentu (pusty, jeśli brak)."""
        cached = self._cache.get(url)
        return cached.text if isinstance(cached, _Fetched) else ""

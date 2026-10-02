"""Rejestr producentów: normalizacja nazw, aliasy, oficjalne domeny."""
from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from pathlib import Path

import yaml

from .models import ManufacturerInfo

log = logging.getLogger(__name__)

# Formy prawne i "szum" usuwany z końca nazwy (wielokrotnie).
LEGAL_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED",
    "LLC", "LLP", "GMBH", "AG", "SA", "SAS", "NV", "BV", "PLC", "KG", "SE", "SPA",
    "SRL", "KK", "OY", "AB", "AS", "PTE", "PTY", "THE", "GROUP", "HOLDINGS",
    "INTERNATIONAL", "INTL",
}
_GENERIC_NAMES = {"", "GENERIC", "ANY", "NA", "N A", "TBD", "VARIOUS", "UNKNOWN", "DNP", "NONE"}


def normalize_name(name: str) -> str:
    """'Texas Instruments Inc.' -> 'TEXAS INSTRUMENTS INC' (pełna, znormalizowana forma)."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    s = s.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_legal(normalized: str) -> str:
    tokens = normalized.split()
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    while len(tokens) > 1 and tokens[0] == "THE":
        tokens.pop(0)
    return " ".join(tokens)


def is_generic_manufacturer(name: str) -> bool:
    return normalize_name(name) in _GENERIC_NAMES


class ManufacturerRegistry:
    def __init__(self, manufacturers: list[ManufacturerInfo], fuzzy: bool = True, cutoff: float = 0.88):
        self.manufacturers = {m.key: m for m in manufacturers}
        self.fuzzy = fuzzy
        self.cutoff = cutoff
        self._index: dict[str, str] = {}
        for m in manufacturers:
            for alias in [m.name, m.key.replace("_", " "), *m.aliases]:
                for form in {normalize_name(alias), strip_legal(normalize_name(alias))}:
                    if not form:
                        continue
                    other = self._index.get(form)
                    if other and other != m.key:
                        raise ValueError(f"Alias '{alias}' wskazuje na dwóch producentów: {other} i {m.key}")
                    self._index[form] = m.key

    def add(self, m: ManufacturerInfo) -> list[str]:
        """Dodaje producenta w trakcie działania (np. wykrytego automatycznie). Zwraca pominięte aliasy."""
        skipped = []
        if m.key in self.manufacturers:
            return skipped
        self.manufacturers[m.key] = m
        for alias in [m.name, *m.aliases]:
            for form in {normalize_name(alias), strip_legal(normalize_name(alias))}:
                if not form:
                    continue
                if self._index.get(form, m.key) != m.key:
                    skipped.append(alias)
                    continue
                self._index[form] = m.key
        return skipped

    def merge_yaml(self, path: str | Path) -> int:
        """Dołącza producentów z dodatkowego pliku (np. discovered_manufacturers.yaml)."""
        path = Path(path)
        if not path.is_file():
            return 0
        extra = ManufacturerRegistry._load_entries(path)
        for m in extra:
            self.add(m)
        return len(extra)

    @classmethod
    def from_yaml(cls, path: str | Path, fuzzy: bool = True, cutoff: float = 0.88) -> "ManufacturerRegistry":
        return cls(cls._load_entries(path), fuzzy=fuzzy, cutoff=cutoff)

    @staticmethod
    def _load_entries(path: str | Path) -> list[ManufacturerInfo]:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        items = []
        for key, entry in (data.get("manufacturers") or {}).items():
            if not entry.get("domains"):
                raise ValueError(f"Producent '{key}' nie ma zdefiniowanych domen (domains)")
            items.append(
                ManufacturerInfo(
                    key=key,
                    name=entry.get("name", key),
                    domains=[d.lower() for d in entry["domains"]],
                    adapter=entry.get("adapter", "generic"),
                    aliases=entry.get("aliases", []) or [],
                    compliance_pages=entry.get("compliance_pages", []) or [],
                    contact_pages=entry.get("contact_pages", []) or [],
                    search_urls=entry.get("search_urls", []) or [],
                    general_documents=entry.get("general_documents", []) or [],
                    product_pages=entry.get("product_pages", []) or [],
                    longevity_pages=entry.get("longevity_pages", []) or [],
                    longevity_documents=entry.get("longevity_documents", []) or [],
                    auto_discovered=entry.get("auto_discovered") or {},
                )
            )
        return items

    def resolve(self, raw_name: str) -> tuple[ManufacturerInfo | None, str]:
        """Zwraca (producent, metoda dopasowania): exact / stripped / fuzzy / none."""
        full = normalize_name(raw_name)
        if not full:
            return None, "none"
        if full in self._index:
            return self.manufacturers[self._index[full]], "exact"
        stripped = strip_legal(full)
        if stripped in self._index:
            return self.manufacturers[self._index[stripped]], "stripped"
        if self.fuzzy and len(stripped) >= 4:
            keys = [k for k in self._index if len(k) >= 4]
            match = difflib.get_close_matches(stripped, keys, n=1, cutoff=self.cutoff)
            if match:
                m = self.manufacturers[self._index[match[0]]]
                log.warning("Przybliżone dopasowanie producenta: '%s' -> %s", raw_name, m.name)
                return m, "fuzzy"
        return None, "none"

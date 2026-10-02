"""Ładowanie konfiguracji (YAML + nadpisania z CLI)."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PACKAGE_ROOT / "config" / "default.yaml"
DEFAULT_MANUFACTURERS = PACKAGE_ROOT / "config" / "manufacturers.yaml"


@dataclass
class Settings:
    # Ścieżki
    output_dir: str = "output"
    manufacturers_file: str = str(DEFAULT_MANUFACTURERS)

    # Mapowanie kolumn BoM (logiczna nazwa -> nazwa kolumny w pliku). Puste = autodetekcja.
    columns: dict[str, str] = field(default_factory=dict)
    sheet: str | None = None

    # HTTP
    # Część serwisów (Akamai/Cloudflare) "zawiesza" połączenia od nieprzeglądarkowych klientów,
    # dlatego domyślnie przedstawiamy się jak przeglądarka. Tempo i robots.txt nadal respektujemy.
    user_agent: str = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36")
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    min_delay_per_host: float = 2.0  # sekundy między zapytaniami do tego samego hosta
    delay_jitter: float = 0.5
    max_retries: int = 4
    backoff_base: float = 2.0
    backoff_max: float = 60.0
    max_redirects: int = 5
    max_download_mb: float = 50.0
    respect_robots: bool = True  # jawne zakazy (Disallow) w robots.txt są zawsze respektowane
    robots_unreachable_policy: str = "allow"  # robots.txt nieosiągalny (timeout/5xx): "allow" albo "disallow" (RFC 9309)
    robots_timeout: float = 8.0  # krótki timeout dla robots.txt
    skip_dead_hosts: bool = True  # host, który nie odpowiada, pomijamy do końca przebiegu (bez kolejnych timeoutów)

    # Logika wyszukiwania
    fuzzy_manufacturer_match: bool = True
    fuzzy_cutoff: float = 0.88
    download_general_statements: bool = True
    count_general_as_success: bool = False
    count_family_as_success: bool = True
    generic_max_pages: int = 8
    generic_max_depth: int = 2
    max_docs_per_item: int = 6
    pdf_text_pages: int = 6  # ile stron PDF analizować przy klasyfikacji

    # Analiza MPN / BoM
    inspect_mpn: bool = True             # sprawdź na stronie producenta, czy MPN jest pełny czy skrócony
    expand_abbreviated_mpn: bool = True  # rozwiń skrót, gdy wariant jest jednoznaczny (zawsze oznaczane w raporcie)
    include_alternates: bool = True      # uwzględnij zamienniki (kolumny "Manufacturer 2 / MPN 2")

    # Dystrybutorzy (zapasowe źródło) i automatyczne wykrywanie producentów
    distributor_fallback: bool = True   # gdy strona producenta nie dała dokumentu dla MPN – szukaj u dystrybutorów
    distributor_sources: list = field(default_factory=lambda: ["nexar", "digikey", "mouser", "tme"])
    distributor_lookup_always: bool = False  # odpytuj dystrybutorów o statusy także dla kompletnych pozycji
    api_keys: dict = field(default_factory=dict)  # alternatywa dla zmiennych środowiskowych (nie commituj!)
    credentials_file: str = str(PACKAGE_ROOT / "config" / "credentials.yaml")  # klucze API (wzór: credentials.example.yaml)
    auto_discover_manufacturers: bool = True  # producent spoza rejestru -> wykryj i zweryfikuj jego domenę
    discovered_manufacturers_file: str = str(PACKAGE_ROOT / "config" / "discovered_manufacturers.yaml")

    # Opcje dodatkowe: cykl życia i longevity
    check_lifecycle: bool = True       # status Active / NRND / EOL ze strony producenta
    check_longevity: bool = True       # program longevity / deklaracja długości produkcji
    request_longevity_by_email: bool = True  # brak deklaracji longevity -> pozycja na liście mailowej
    save_lifecycle_snapshots: bool = True  # zapisuj kopię strony, z której odczytano status
    download_longevity_documents: bool = True  # pobieraj polityki EOL / longevity producenta

    # Raport / e-mail
    requester_name: str = "[Your Name]"
    requester_company: str = "[Your Company]"
    requester_email: str = "[your.email@company.com]"
    project_name: str = "[Product / Project name]"

    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> "Settings":
        data: dict[str, Any] = {}
        for p in (DEFAULT_CONFIG, path):
            if p and Path(p).is_file():
                with open(p, encoding="utf-8") as fh:
                    loaded = yaml.safe_load(fh) or {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"Plik konfiguracyjny {p} musi zawierać mapę klucz: wartość")
                data.update(loaded)
            elif p and p is path:
                raise FileNotFoundError(f"Nie znaleziono pliku konfiguracyjnego: {p}")
        for k, v in (overrides or {}).items():
            if v is None:
                continue
            if k == "columns":
                data.setdefault("columns", {})
                data["columns"] = {**(data.get("columns") or {}), **v}
            else:
                data[k] = v
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Nieznane klucze konfiguracji: {', '.join(sorted(unknown))}")
        return cls(**data)

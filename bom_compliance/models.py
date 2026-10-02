"""Wspólne struktury danych używane przez wszystkie moduły."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class DocType(str, enum.Enum):
    ROHS = "RoHS"
    REACH = "REACH"
    MCD = "MCD"  # material content / composition declaration (IPC-1752 itp.)
    LONGEVITY = "LONGEVITY"  # deklaracja / polityka długości produkcji (longevity, EOL policy)


class Scope(str, enum.Enum):
    PART = "part"        # dokument wystawiony dla konkretnego MPN
    FAMILY = "family"    # dokument zbiorczy dla rodziny / serii / obudowy
    GENERAL = "general"  # ogólne oświadczenie producenta (cały portfel)


class Status(str, enum.Enum):
    DOWNLOADED = "DOWNLOADED"                 # dokument dla konkretnego MPN
    DOWNLOADED_FAMILY = "DOWNLOADED_FAMILY"   # dokument zbiorczy dla rodziny
    GENERAL_ONLY = "GENERAL_ONLY"             # tylko ogólne oświadczenie producenta
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    FORM_REQUIRED = "FORM_REQUIRED"
    BLOCKED_ROBOTS = "BLOCKED_ROBOTS"
    NOT_FOUND = "NOT_FOUND"
    ERROR = "ERROR"
    UNKNOWN_MANUFACTURER = "UNKNOWN_MANUFACTURER"


@dataclass
class ManufacturerInfo:
    """Wpis z rejestru producentów (config/manufacturers.yaml)."""

    key: str
    name: str
    domains: list[str]
    adapter: str = "generic"
    aliases: list[str] = field(default_factory=list)
    compliance_pages: list[str] = field(default_factory=list)
    contact_pages: list[str] = field(default_factory=list)
    search_urls: list[str] = field(default_factory=list)
    general_documents: list[dict] = field(default_factory=list)
    product_pages: list[str] = field(default_factory=list)  # szablony stron produktu ({mpn}, {base})
    longevity_pages: list[str] = field(default_factory=list)  # strony / listy programu longevity
    longevity_documents: list[dict] = field(default_factory=list)  # polityki EOL / longevity (PDF)


@dataclass
class BomRow:
    row_number: int  # numer wiersza w pliku źródłowym (1-based, z nagłówkiem)
    mpn: str
    manufacturer: str
    refdes: list[str] = field(default_factory=list)
    quantity: str = ""
    sheet: str = ""
    mpn_raw: str = ""            # dokładna zawartość komórki MPN
    alternate: bool = False      # zamiennik (2. źródło) z kolumn "Manufacturer 2 / MPN 2" lub z tej samej komórki
    wildcard: bool = False
    hints: list[str] = field(default_factory=list)  # pełne numery znalezione w innych kolumnach (opis)
    notes: list[str] = field(default_factory=list)


@dataclass
class InvalidRow:
    row_number: int
    reason: str
    raw: dict


@dataclass
class BomItem:
    """Unikalna para (producent, MPN) po deduplikacji."""

    mpn: str
    manufacturer_raw: str
    manufacturer: ManufacturerInfo | None
    match_method: str  # exact / stripped / fuzzy / none
    refdes: list[str] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    manufacturer_variants: list[str] = field(default_factory=list)
    mpn_bom: str = ""            # MPN z BoM po oczyszczeniu (mpn = numer używany do wyszukiwania)
    mpn_raw: list[str] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    alternate: bool = False      # pozycja występuje wyłącznie jako zamiennik
    wildcard: bool = False
    hints: list[str] = field(default_factory=list)
    mpn_notes: list[str] = field(default_factory=list)
    mpn_check: object | None = None  # mpn.MpnCheck

    @property
    def manufacturer_name(self) -> str:
        return self.manufacturer.name if self.manufacturer else self.manufacturer_raw


@dataclass
class Candidate:
    """Kandydat na dokument znaleziony przez adapter (jeszcze niepobrany)."""

    url: str
    doc_types: set[DocType]
    scope: Scope
    title: str = ""
    source_page: str = ""
    allow_html: bool = False  # czy strona HTML sama w sobie jest dokumentem
    note: str = ""
    fixed_types: bool = False  # nie klasyfikuj rodzaju z treści (np. polityka longevity)


@dataclass
class SearchResult:
    candidates: list[Candidate] = field(default_factory=list)
    login_required: list[str] = field(default_factory=list)
    form_required: list[str] = field(default_factory=list)
    robots_blocked: list[str] = field(default_factory=list)
    manual_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add(self, cand: Candidate) -> None:
        if all(c.url != cand.url for c in self.candidates):
            self.candidates.append(cand)


@dataclass
class DownloadedDoc:
    url: str
    final_url: str
    path: str
    sha256: str
    downloaded_at: str
    content_type: str
    doc_types: set[DocType]
    scope: Scope
    mpn_verified: str  # "yes" / "no" / "unknown"
    title: str = ""
    note: str = ""
    shared: bool = False  # ten sam plik użyty dla wielu pozycji BoM
    paths: dict = field(default_factory=dict)  # DocType -> ścieżka kopii w folderze danego rodzaju

    def path_for(self, doc_type: "DocType") -> str:
        return self.paths.get(doc_type, self.path)


class LifecycleStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"                # w produkcji / zalecany
    PREVIEW = "PREVIEW"              # przed produkcją (preview, sampling, proposal)
    MATURE = "MATURE"                # dojrzały – w produkcji, ale bez rozwoju
    NRND = "NRND"                    # not recommended for new designs
    LAST_TIME_BUY = "LAST_TIME_BUY"  # ogłoszony koniec produkcji, okres ostatnich zamówień
    OBSOLETE = "OBSOLETE"            # EOL / discontinued / obsolete
    UNKNOWN = "UNKNOWN"


@dataclass
class LifecycleInfo:
    status: LifecycleStatus = LifecycleStatus.UNKNOWN
    label: str = ""           # dokładna etykieta ze strony producenta (np. "Not Recommended for new designs")
    scope: str = ""           # "part" – etykieta przy dokładnym MPN; "page" – status strony produktu/rodziny
    source_url: str = ""
    evidence: str = ""        # fragment tekstu, z którego odczytano status
    checked_at: str = ""
    snapshot: str = ""        # ścieżka do zapisanej kopii strony (dowód)
    note: str = ""


@dataclass
class LongevityInfo:
    found: bool = False
    program: str = ""         # nazwa programu / polityki
    years: int | None = None  # zadeklarowany okres (lata)
    start_year: int | None = None
    end_year: int | None = None   # do kiedy (jawnie lub start + lata)
    end_basis: str = ""       # "explicit" / "start+years" / ""
    scope: str = ""           # part / family / general
    source_url: str = ""
    evidence: str = ""
    docs: list["DownloadedDoc"] = field(default_factory=list)
    snapshot: str = ""        # kopia strony (lista longevity), z której odczytano deklarację
    note: str = ""


@dataclass
class ItemResult:
    item: BomItem
    docs: list[DownloadedDoc] = field(default_factory=list)
    rohs_status: Status = Status.NOT_FOUND
    reach_status: Status = Status.NOT_FOUND
    reasons: list[str] = field(default_factory=list)
    manual_urls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lifecycle: LifecycleInfo | None = None
    longevity: LongevityInfo | None = None

    def docs_for(self, doc_type: DocType) -> list[DownloadedDoc]:
        order = {Scope.PART: 0, Scope.FAMILY: 1, Scope.GENERAL: 2}
        return sorted((d for d in self.docs if doc_type in d.doc_types), key=lambda d: order[d.scope])

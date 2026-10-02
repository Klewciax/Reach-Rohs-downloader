"""Raport końcowy: CSV + XLSX + szablony e-maili + podsumowanie w konsoli."""
from __future__ import annotations

import csv
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .contacts import ContactInfo
from .downloader import safe_name
from .models import DocType, InvalidRow, ItemResult, LifecycleStatus, Scope, Status
from .pipeline import is_success

log = logging.getLogger(__name__)

STATUS_PL = {
    Status.DOWNLOADED: "POBRANO (dokument dla MPN)",
    Status.DOWNLOADED_FAMILY: "POBRANO – DOKUMENT ZBIORCZY (rodzina)",
    Status.GENERAL_ONLY: "TYLKO OGÓLNE OŚWIADCZENIE PRODUCENTA",
    Status.LOGIN_REQUIRED: "WYMAGA LOGOWANIA",
    Status.FORM_REQUIRED: "DOSTĘPNE PRZEZ FORMULARZ",
    Status.BLOCKED_ROBOTS: "ZABLOKOWANE (robots.txt)",
    Status.NOT_FOUND: "NIE ZNALEZIONO",
    Status.ERROR: "BŁĄD",
    Status.UNKNOWN_MANUFACTURER: "NIEZNANY PRODUCENT",
}

LIFECYCLE_PL = {
    LifecycleStatus.ACTIVE: "ACTIVE (w produkcji)",
    LifecycleStatus.PREVIEW: "PREVIEW (przed produkcją)",
    LifecycleStatus.MATURE: "MATURE (dojrzały)",
    LifecycleStatus.NRND: "NRND (niezalecany do nowych projektów)",
    LifecycleStatus.LAST_TIME_BUY: "LAST TIME BUY (ostatnie zamówienia)",
    LifecycleStatus.OBSOLETE: "EOL / OBSOLETE (wycofany)",
    LifecycleStatus.UNKNOWN: "NIEZNANY – do ręcznej weryfikacji",
}
RISKY = {LifecycleStatus.NRND, LifecycleStatus.LAST_TIME_BUY, LifecycleStatus.OBSOLETE}

LIFECYCLE_COLUMNS = [
    "Status cyklu życia", "Etykieta producenta", "Zakres statusu", "URL statusu", "Dowód (fragment strony)",
    "Kopia strony", "Sprawdzono (UTC)", "Uwagi (cykl życia)",
]
LONGEVITY_COLUMNS = [
    "Longevity: znaleziono", "Program / dokument", "Okres (lata)", "Od (rok)", "Do kiedy (rok)", "Podstawa daty",
    "Zakres longevity", "URL longevity", "Plik longevity", "Dowód longevity", "Uwagi (longevity)",
]

ITEM_COLUMNS = [
    "MPN", "Manufacturer (BoM)", "Manufacturer (znormalizowany)", "Dopasowanie producenta", "Ref. designators",
    "Wiersz(e) w pliku BoM", "Arkusz", "Zamiennik (2. źródło)", "Komórka MPN (oryginał)", "Forma MPN",
    "MPN użyty do wyszukiwania", "Numery u producenta zaczynające się od MPN", "Uwagi do MPN", "Status RoHS", "Status REACH", "Plik RoHS", "URL źródłowy RoHS", "Zakres RoHS",
    "Plik REACH", "URL źródłowy REACH", "Zakres REACH", "Data pobrania (UTC)", "MPN potwierdzony w treści",
    "Powód niepowodzenia / uwagi", "Do ręcznego sprawdzenia (oficjalne strony)",
]


@dataclass
class Summary:
    bom_rows_total: int
    bom_rows_valid: int
    bom_rows_invalid: int
    items: int
    rohs: int
    reach: int
    both: int
    none: int
    family_docs: int
    extra: dict = field(default_factory=dict)

    def pct(self, n: int) -> str:
        return f"{(100.0 * n / self.items):.1f}%" if self.items else "0.0%"


def summarize(results: list[ItemResult], settings: Settings, meta: dict, invalid: list[InvalidRow]) -> Summary:
    s = _summarize(results, settings, meta, invalid)
    checks = [getattr(r.item.mpn_check, "form", "") for r in results]
    s.extra["MPN skrócone (nazwa produktu/rodziny)"] = sum(1 for f in checks if f.startswith("skrócony"))
    s.extra["  w tym rozwinięte automatycznie do pełnego numeru"] = sum(
        1 for r in results if getattr(r.item.mpn_check, "expanded_to", ""))
    s.extra["MPN-wzorce rodzin (xxx, *)"] = sum(1 for r in results if r.item.wildcard)
    s.extra["MPN nieznalezione na stronie producenta"] = sum(1 for f in checks if f.startswith("nie znaleziono"))
    s.extra["Pozycje będące wyłącznie zamiennikami (2. źródło)"] = sum(1 for r in results if r.item.alternate)
    s.extra["Producenci spoza rejestru wykryci automatycznie (pozycje)"] = sum(
        1 for r in results if r.item.match_method == "auto")
    s.extra["Pozycje z plikiem RoHS/REACH od dystrybutora"] = sum(
        1 for r in results if any(d.source and d.scope != Scope.GENERAL for d in r.docs))
    if settings.check_lifecycle:
        for st in LifecycleStatus:
            n = sum(1 for r in results if r.lifecycle and r.lifecycle.status == st)
            s.extra[f"Cykl życia: {LIFECYCLE_PL[st]}"] = n
    if settings.check_longevity:
        s.extra["Longevity: ustalono datę 'do kiedy'"] = sum(
            1 for r in results if r.longevity and r.longevity.found and r.longevity.end_year)
        s.extra["Longevity: brak deklaracji dla MPN/rodziny"] = sum(
            1 for r in results if not (r.longevity and r.longevity.found))
    return s


def _summarize(results: list[ItemResult], settings: Settings, meta: dict, invalid: list[InvalidRow]) -> Summary:
    rohs = sum(is_success(r.rohs_status, settings) for r in results)
    reach = sum(is_success(r.reach_status, settings) for r in results)
    both = sum(is_success(r.rohs_status, settings) and is_success(r.reach_status, settings) for r in results)
    none = sum(not is_success(r.rohs_status, settings) and not is_success(r.reach_status, settings) for r in results)
    fam = sum(Status.DOWNLOADED_FAMILY in (r.rohs_status, r.reach_status) for r in results)
    return Summary(meta.get("total_rows", 0), meta.get("total_rows", 0) - len(invalid), len(invalid),
                   len(results), rohs, reach, both, none, fam)


def _doc_cells(r: ItemResult, t: DocType) -> tuple[str, str, str, str]:
    docs = r.docs_for(t)
    if docs:  # pokazuj tylko dokumenty o najlepszym (najwęższym) zakresie
        docs = [d for d in docs if d.scope == docs[0].scope]
    return ("\n".join(d.path_for(t) for d in docs), "\n".join(d.final_url if d.final_url == d.url else
                                                       f"{d.url} -> {d.final_url}" for d in docs),
            "\n".join(d.scope.value for d in docs), "\n".join(dict.fromkeys(d.downloaded_at for d in docs)))


def lifecycle_cells(r: ItemResult) -> list[str]:
    lc = r.lifecycle
    if lc is None:
        return [""] * len(LIFECYCLE_COLUMNS)
    scope = {"part": "dla MPN", "page": "strona produktu / rodziny"}.get(lc.scope, "")
    return [LIFECYCLE_PL[lc.status], lc.label, scope, lc.source_url, lc.evidence, lc.snapshot, lc.checked_at, lc.note]


def longevity_cells(r: ItemResult) -> list[str]:
    lg = r.longevity
    if lg is None:
        return [""] * len(LONGEVITY_COLUMNS)
    scope = {"part": "dla MPN", "family": "rodzina (prefiks MPN)", "general": "ogólna polityka"}.get(lg.scope, lg.scope)
    return ["TAK" if lg.found else "NIE", lg.program, "" if lg.years is None else str(lg.years),
            "" if lg.start_year is None else str(lg.start_year), "" if lg.end_year is None else str(lg.end_year),
            lg.end_basis, scope, lg.source_url, "\n".join(d.path for d in lg.docs), lg.evidence, lg.note]


def item_columns(settings: Settings) -> list[str]:
    cols = list(ITEM_COLUMNS)
    if settings.check_lifecycle:
        cols += LIFECYCLE_COLUMNS
    if settings.check_longevity:
        cols += LONGEVITY_COLUMNS
    return cols


def mpn_cells(r: ItemResult) -> list[str]:
    it = r.item
    chk = it.mpn_check
    form = getattr(chk, "form", "") or ("wzorzec rodziny" if it.wildcard else "nie sprawdzono")
    notes = list(it.mpn_notes) + list(getattr(chk, "notes", []) or [])
    if getattr(chk, "source_url", ""):
        notes.append(f"źródło: {chk.source_url}")
    if it.hints:
        notes.append("w opisie BoM: " + ", ".join(it.hints))
    return [", ".join(it.sheets), "TAK" if it.alternate else "", " | ".join(dict.fromkeys(it.mpn_raw)), form,
            it.mpn, ", ".join(getattr(chk, "variants", []) or []), "\n".join(dict.fromkeys(notes))]


def item_rows(results: list[ItemResult], settings: Settings | None = None) -> list[list[str]]:
    rows = []
    for r in results:
        rp, ru, rs, rd = _doc_cells(r, DocType.ROHS)
        ep, eu, es, ed = _doc_cells(r, DocType.REACH)
        dates = "\n".join(dict.fromkeys(x for x in (rd + "\n" + ed).split("\n") if x))
        verified = "; ".join(dict.fromkeys(f"{Path(d.path).name}: {d.mpn_verified}" for d in r.docs))
        reasons = r.reasons + [n for n in r.notes if n not in r.reasons]
        for d in r.docs:
            if d.note:
                reasons.append(f"{Path(d.path).name}: {d.note}")
        rows.append([
            r.item.mpn_bom or r.item.mpn, " | ".join(r.item.manufacturer_variants) or r.item.manufacturer_raw,
            r.item.manufacturer.name if r.item.manufacturer else "", r.item.match_method,
            ", ".join(r.item.refdes), ", ".join(map(str, r.item.rows)), *mpn_cells(r),
            STATUS_PL[r.rohs_status], STATUS_PL[r.reach_status], rp, ru, rs, ep, eu, es, dates, verified,
            "\n".join(reasons), "\n".join(r.manual_urls),
        ])
        if settings is not None and settings.check_lifecycle:
            rows[-1] += lifecycle_cells(r)
        if settings is not None and settings.check_longevity:
            rows[-1] += longevity_cells(r)
    return rows


def file_rows(results: list[ItemResult]) -> list[list[str]]:
    seen: "OrderedDict[str, list]" = OrderedDict()
    for r in results:
        for d in r.docs + (r.longevity.docs if r.longevity else []):
            for path in dict.fromkeys(list(d.paths.values()) or [d.path]):
                if path not in seen:
                    seen[path] = [path, d.url, d.final_url, d.downloaded_at, d.sha256,
                                  ", ".join(sorted(t.value for t in d.doc_types)), d.scope.value,
                                  r.item.manufacturer_name, [], d.title]
                if r.item.mpn not in seen[path][8]:
                    seen[path][8].append(r.item.mpn)
    return [[*v[:8], ", ".join(v[8]), v[9]] for v in seen.values()]


@dataclass
class RequestGroup:
    manufacturer: str
    items: list[tuple[str, list[tuple[str, bool]], str]]  # (MPN, brakujące typy, status)
    contact: ContactInfo | None
    email: str = ""


def missing_types(r: ItemResult, settings: Settings) -> list[tuple[str, bool]]:
    """Lista (rodzaj, czy_jest_tylko_dokument_zbiorczy) dla dokumentów do uzyskania od producenta."""
    out = []
    for name, st in (("RoHS", r.rohs_status), ("REACH", r.reach_status)):
        if st == Status.DOWNLOADED_FAMILY:
            out.append((name, True))  # prosimy o deklarację dla konkretnego MPN
        elif not is_success(st, settings):
            out.append((name, False))
    if settings.check_longevity and settings.request_longevity_by_email and r.item.manufacturer is not None:
        lg = r.longevity
        if lg is None or not lg.found or lg.end_year is None:
            out.append(("Longevity", False))
        elif lg.scope != "part":
            out.append(("Longevity", True))
    return out


def _missing_pl(miss: list[tuple[str, bool]]) -> str:
    names = {"Longevity": "Longevity (deklaracja długości produkcji)"}
    return ", ".join(f"{names.get(t, t)} (jest tylko dokument zbiorczy – potrzebna deklaracja dla MPN)" if fam
                     else names.get(t, t) for t, fam in miss)


def _missing_en(miss: list[tuple[str, bool]]) -> str:
    names = {"Longevity": "longevity / end-of-production date"}
    return ", ".join(f"{names.get(t, t)} – part-specific (only a product-family document found)" if fam
                     else names.get(t, t) for t, fam in miss)


def build_request_groups(results: list[ItemResult], settings: Settings,
                         contacts: dict[str, ContactInfo]) -> list[RequestGroup]:
    groups: "OrderedDict[str, RequestGroup]" = OrderedDict()
    for r in results:
        miss = missing_types(r, settings)
        if not miss:
            continue
        name = r.item.manufacturer_name
        key = r.item.manufacturer.key if r.item.manufacturer else "?" + name
        g = groups.get(key)
        if g is None:
            g = groups[key] = RequestGroup(name, [], contacts.get(key))
        status = f"RoHS: {STATUS_PL[r.rohs_status]}; REACH: {STATUS_PL[r.reach_status]}"
        label = r.item.mpn if r.item.mpn == (r.item.mpn_bom or r.item.mpn) else \
            f"{r.item.mpn} (BoM: {r.item.mpn_bom})"
        if getattr(r.item.mpn_check, "form", "").startswith("skrócony") and not r.item.mpn_check.expanded_to:
            label += " [base part number – please cover all orderable variants]"
        elif r.item.wildcard:
            label += " [family pattern – please cover all matching part numbers]"
        g.items.append((label, miss, status))
    for g in groups.values():
        g.email = email_template(g, settings)
    return list(groups.values())


def email_template(g: RequestGroup, s: Settings) -> str:
    lines = [f"  - {mpn}  (needed: {_missing_en(m)})" for mpn, m, _ in g.items]
    kinds = {t for _, m, _ in g.items for t, _ in m}
    compliance = bool(kinds & {"RoHS", "REACH"})
    longevity = "Longevity" in kinds
    asks: list[str] = []
    if compliance:
        asks += [
            "EU RoHS declaration / certificate of compliance (Directive 2011/65/EU as amended by\n"
            "     (EU) 2015/863), stating any RoHS exemptions used (Annex III/IV item numbers).",
            "EU REACH declaration (Regulation (EC) No 1907/2006), including the SVHC status\n"
            "     against the latest ECHA Candidate List: name and CAS number of any SVHC present\n"
            "     above 0.1% w/w, its concentration and location in the article, and the Candidate\n"
            "     List version the statement refers to.",
            "If available, a full material declaration (IPC-1752A Class D or equivalent).",
        ]
    if longevity:
        asks += [
            "Current product lifecycle status (e.g. active / NRND / last-time-buy / obsolete) and\n"
            "     a longevity statement: the guaranteed minimum production / availability period and\n"
            "     the date until which the part is planned to remain in production, including whether\n"
            "     the part is covered by a formal longevity programme and your EOL notification policy.",
        ]
    numbered = "\n".join(f"  {i}. {a}" for i, a in enumerate(asks, 1))
    topics = " and ".join(x for x, on in (("RoHS and REACH (SVHC) compliance declarations", compliance),
                                           ("product longevity information", longevity)) if on)
    return f"""Subject: Request for {topics} – {len(g.items)} {g.manufacturer} part number(s)

Dear {g.manufacturer} Product Compliance / Environmental Team,

We use the following {g.manufacturer} components in our product "{s.project_name}" and are
compiling the compliance and supply documentation for it. We were unable to obtain
part-specific information for these part numbers from your official website:

{chr(10).join(lines)}

For each part number listed above, could you please provide:

{numbered}

The documents should be issued by {g.manufacturer} as the manufacturer and clearly reference
the exact manufacturer part numbers (orderable part numbers) listed above. A signed PDF
is preferred.

Thank you in advance for your support.

Best regards,
{s.requester_name}
{s.requester_company}
{s.requester_email}
"""


def write_reports(out_dir: Path, results: list[ItemResult], invalid: list[InvalidRow], meta: dict,
                  settings: Settings, groups: list[RequestGroup], summary: Summary) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    items = item_rows(results, settings)
    files = file_rows(results)
    item_header = item_columns(settings)

    def write_csv(name: str, header: list[str], rows: list[list]) -> Path:
        p = out_dir / name
        with open(p, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(header)
            w.writerows(rows)
        return p

    paths["items_csv"] = write_csv("report_items.csv", item_header, items)
    file_header = ["Ścieżka", "URL źródłowy", "URL końcowy", "Data pobrania (UTC)", "SHA-256", "Rodzaj",
                   "Zakres", "Producent", "Użyty dla MPN", "Tytuł"]
    paths["files_csv"] = write_csv("report_files.csv", file_header, files)
    req_header = ["Producent", "MPN", "Do uzyskania", "Status", "Kontakt (oficjalna strona producenta)"]
    req_rows = []
    for g in groups:
        contact = g.contact.summary() if g.contact else \
            "DO RĘCZNEJ WERYFIKACJI: producent spoza rejestru – brak zweryfikowanej domeny"
        for mpn, miss, status in g.items:
            req_rows.append([g.manufacturer, mpn, _missing_pl(miss), status, contact])
    paths["requests_csv"] = write_csv("report_to_request.csv", req_header, req_rows)

    tpl_dir = out_dir / "email_templates"
    tpl_dir.mkdir(exist_ok=True)
    for g in groups:
        p = tpl_dir / f"{safe_name(g.manufacturer)}.txt"
        contact = g.contact.summary() if g.contact else "DO RĘCZNEJ WERYFIKACJI"
        p.write_text(f"# Kontakt:\n# " + contact.replace("\n", "\n# ") + "\n\n" + g.email, encoding="utf-8")

    lc_rows = lifecycle_rows(results, settings)
    if lc_rows:
        paths["lifecycle_csv"] = write_csv("report_lifecycle.csv", lifecycle_header(settings), lc_rows)
    from .excel_report import write_workbook

    paths["xlsx"] = write_workbook(out_dir / "report.xlsx", out_dir, results, summary_lines(summary), meta, settings,
                                   item_header, items, file_header, files, groups, req_rows, req_header, invalid,
                                   STATUS_PL, LIFECYCLE_PL)
    return paths


def lifecycle_header(settings: Settings) -> list[str]:
    cols = ["MPN", "Producent", "Ref. designators"]
    if settings.check_lifecycle:
        cols += LIFECYCLE_COLUMNS
    if settings.check_longevity:
        cols += LONGEVITY_COLUMNS
    return cols


def lifecycle_rows(results: list[ItemResult], settings: Settings) -> list[list[str]]:
    if not (settings.check_lifecycle or settings.check_longevity):
        return []
    rows = []
    for r in results:
        row = [r.item.mpn, r.item.manufacturer_name, ", ".join(r.item.refdes)]
        if settings.check_lifecycle:
            row += lifecycle_cells(r)
        if settings.check_longevity:
            row += longevity_cells(r)
        rows.append(row)
    return rows


def summary_lines(s: Summary) -> list[tuple[str, str, str]]:
    extra = []
    for label, n in s.extra.items():
        extra.append((label, str(n), s.pct(n)))
    return extra_first(s) + extra


def extra_first(s: Summary) -> list[tuple[str, str, str]]:
    return [
        ("Wiersze BoM (niepuste)", str(s.bom_rows_total), ""),
        ("Wiersze poprawne", str(s.bom_rows_valid), ""),
        ("Wiersze niepoprawne / pominięte", str(s.bom_rows_invalid), ""),
        ("Pozycje BoM po deduplikacji (producent + MPN)", str(s.items), "100%"),
        ("Pobrano RoHS", str(s.rohs), s.pct(s.rohs)),
        ("Pobrano REACH", str(s.reach), s.pct(s.reach)),
        ("Pobrano oba (RoHS i REACH)", str(s.both), s.pct(s.both)),
        ("Nie pobrano żadnego", str(s.none), s.pct(s.none)),
        ("  w tym pozycje z dokumentem zbiorczym (rodzina)", str(s.family_docs), s.pct(s.family_docs)),
    ]


def print_console_summary(s: Summary, groups: list[RequestGroup], paths: dict[str, Path], results: list[ItemResult]
                          ) -> None:
    w = 52
    print("\n" + "=" * 72)
    print("RAPORT RoHS / REACH" + (" / CYKL ŻYCIA" if any(r.lifecycle for r in results) else "")
          + (" / LONGEVITY" if any(r.longevity for r in results) else ""))
    print("=" * 72)
    for label, n, pct in summary_lines(s):
        print(f"{label:<{w}} {n:>6}  {pct:>7}")
    status_counts: dict[str, int] = {}
    for r in results:
        for st in (r.rohs_status, r.reach_status):
            status_counts[st.value] = status_counts.get(st.value, 0) + 1
    print("-" * 72)
    print("Statusy (RoHS + REACH łącznie): " + ", ".join(f"{k}={v}" for k, v in sorted(status_counts.items())))
    risky = [r for r in results if r.lifecycle and r.lifecycle.status in RISKY]
    if risky:
        print("-" * 72)
        print("UWAGA – komponenty NRND / Last Time Buy / EOL (wg strony producenta):")
        for r in risky:
            print(f"  {r.item.manufacturer_name} {r.item.mpn}: {LIFECYCLE_PL[r.lifecycle.status]} "
                  f"('{r.lifecycle.label}', {r.lifecycle.source_url})")
    ending = [r for r in results if r.longevity and r.longevity.end_year]
    if ending:
        print("-" * 72)
        print("Longevity (zadeklarowana dostępność do roku):")
        for r in sorted(ending, key=lambda x: x.longevity.end_year):
            print(f"  {r.item.manufacturer_name} {r.item.mpn}: do {r.longevity.end_year} "
                  f"[{r.longevity.scope}; {r.longevity.end_basis}]")
    if groups:
        print("-" * 72)
        print("Do uzyskania mailowo:")
        for g in groups:
            print(f"  {g.manufacturer}: {len(g.items)} MPN – " + ", ".join(m for m, _, _ in g.items[:8])
                  + (" …" if len(g.items) > 8 else ""))
    print("-" * 72)
    if "xlsx" in paths:
        print(f"RAPORT GŁÓWNY (Excel): {paths['xlsx']}")
        print(f"Pobrane pliki:         {paths['xlsx'].parent / 'documents'}  (RoHS / REACH / Status_cyklu_zycia / "
              "Dlugosc_produkcji)")
    for k, p in paths.items():
        if k != "xlsx":
            print(f"{k:<14} {p}")
    print("=" * 72)

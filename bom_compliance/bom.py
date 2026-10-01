"""Wczytywanie BoM (CSV/XLSX/XLS): analiza arkuszy, wykrywanie kolumn, walidacja, deduplikacja.

Typowe BoM-y z Excela mają kilka kart (okładka, właściwa lista, historia zmian), nagłówek
nie w pierwszym wierszu, nietypowe nazwy kolumn ("Nr kat.", "Producent") oraz kolumny
zamienników ("Manufacturer 2" / "MPN 2"). Moduł:
  1. analizuje KAŻDY arkusz i wybiera ten, który faktycznie zawiera listę części
     (lub łączy wszystkie takie arkusze: sheet="all"),
  2. wykrywa kolumny po nagłówkach, a gdy to się nie uda – po zawartości (wartości
     rozpoznawane jako producenci z rejestru, wartości wyglądające jak MPN, ref. designatory),
  3. weryfikuje mapowanie po nagłówkach zawartością (np. "Part Number" z numerami wewnętrznymi),
  4. czyści wartości MPN (prefiks producenta, opis w komórce, kilka numerów, wzorce xxx / *).
"""
from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .classify import compact
from .manufacturers import ManufacturerRegistry, is_generic_manufacturer, normalize_name, strip_legal
from .models import BomItem, BomRow, InvalidRow
from .mpn import clean_mpn, hints_from_text, is_wildcard, looks_like_mpn

log = logging.getLogger(__name__)

# Typowe nazwy kolumn (porównywane po normalizacji: małe litery, bez znaków niealfanumerycznych).
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "mpn": [
        "mpn", "manufacturer part number", "manufacturer part no", "manufacturer pn", "manufacturers part number",
        "mfr part number", "mfr part no", "mfr part", "mfr pn", "mfr p n", "mfg part number", "mfr part nr",
        "mfg pn", "mfg part no", "manufacturer part", "mfr number", "part number manufacturer",
        "manufacturer part nr", "manufacturer ordering code", "ordering code", "order code manufacturer",
        "numer katalogowy producenta", "nr kat producenta", "numer katalogowy", "nr katalogowy", "nr kat",
        "kod producenta", "symbol producenta", "oznaczenie producenta", "numer producenta", "nr producenta",
        "part number", "partnumber", "pn", "p n", "part no", "part",
    ],
    "manufacturer": [
        "manufacturer", "manufacturer name", "mfr", "mfr name", "mfg", "mfg name", "maker", "make",
        "producent", "nazwa producenta", "wytworca", "wytwórca", "brand", "marka",
    ],
    "refdes": [
        "refdes", "ref des", "reference", "references", "reference designator", "reference designators",
        "designator", "designators", "ref", "refs", "oznaczenie", "oznaczenia", "part reference",
        "pozycja na schemacie", "symbol na schemacie", "elementy",
    ],
    "quantity": ["qty", "quantity", "ilosc", "ilość", "count", "qty per board", "szt", "ilosc szt", "ilość szt"],
}
_ALT_WORDS = re.compile(r"\b(alt|alternate|alternative|alternatywny|zamiennik|second source|2nd source)\b")

INVALID_MPN_VALUES = {"", "NA", "N/A", "-", "--", "TBD", "DNP", "DNF", "NC", "NONE", "NOTUSED", "?", "X", "BRAK"}
_REFDES_RE = re.compile(r"^([A-Z]{1,4}\d{1,4}[A-Z]?)([\s,;:\-]+[A-Z]{1,4}\d{1,4}[A-Z]?)*$", re.I)
MAX_ROWS = 50000


@dataclass
class SheetAnalysis:
    name: str
    hidden: bool = False
    nonempty_rows: int = 0
    status: str = ""
    header_row: int | None = None      # 1-based
    mapping: dict[str, int] = field(default_factory=dict)
    alt_pairs: list[tuple[int, int]] = field(default_factory=list)  # (kolumna producenta, kolumna MPN)
    method: str = ""
    data_rows: int = 0
    headers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        cols = ", ".join(f"{k}='{self.headers[v]}'" for k, v in self.mapping.items() if v < len(self.headers))
        alt = f", zamienniki: {len(self.alt_pairs)} par kolumn" if self.alt_pairs else ""
        hid = " [ukryty]" if self.hidden else ""
        base = f"'{self.name}'{hid}: {self.status}"
        if self.mapping:
            base += f" (nagłówek w wierszu {self.header_row}, {self.data_rows} wierszy danych, " \
                    f"kolumny wg {self.method}: {cols}{alt})"
        return base


# ------------------------------------------------------------------ odczyt

def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v).replace(" ", " ").strip()


def _read_csv(path: Path) -> list[list]:
    raw = path.read_bytes()
    text = raw.decode("latin-1")
    for enc in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    import io

    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _read_xlsx(path: Path) -> list[tuple[str, bool, list[list]]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    out = []
    for ws in wb.worksheets:
        hidden = getattr(ws, "sheet_state", "visible") != "visible"
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_ROWS:
                break
            rows.append(list(row))
        out.append((ws.title, hidden, rows))
    wb.close()
    return out


def _read_xls(path: Path) -> list[tuple[str, bool, list[list]]]:
    try:
        import xlrd
    except ImportError:
        raise ValueError("Pliki .xls (Excel 97-2003) wymagają pakietu xlrd: pip install xlrd "
                         "(albo zapisz plik jako .xlsx)") from None
    book = xlrd.open_workbook(str(path))
    out = []
    for sh in book.sheets():
        rows = [sh.row_values(r) for r in range(min(sh.nrows, MAX_ROWS))]
        out.append((sh.name, getattr(sh, "visibility", 0) != 0, rows))
    return out


def _load_tables(path: Path) -> list[tuple[str, bool, list[list]]]:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        return _read_xlsx(path)
    if suffix == ".xls":
        return _read_xls(path)
    if suffix in (".csv", ".txt", ".tsv"):
        return [("csv", False, _read_csv(path))]
    raise ValueError(f"Nieobsługiwany format BoM: {suffix} (obsługiwane: .xlsx, .xlsm, .xls, .csv, .tsv)")


# --------------------------------------------------------- wykrywanie kolumn

def _norm_header(h: object) -> str:
    s = _cell(h).lower().replace("#", " number ").replace("nº", " nr ").replace("no.", "no ")
    return re.sub(r"[^a-z0-9ąćęłńóśźż]+", " ", s).strip()


def _logical_of(header: str) -> str | None:
    for logical, synonyms in COLUMN_SYNONYMS.items():
        if header in synonyms:
            return logical
    return None


def _detect_by_headers(headers: list, explicit: dict[str, str]) -> dict[str, int]:
    norm = [_norm_header(h) for h in headers]
    mapping: dict[str, int] = {}
    for logical, colname in (explicit or {}).items():
        if not colname:
            continue
        target = _norm_header(colname)
        if target not in norm:
            raise ValueError(f"Kolumna '{colname}' (dla '{logical}') nie istnieje. "
                             f"Dostępne: {', '.join(_cell(h) for h in headers if _cell(h))}")
        mapping[logical] = norm.index(target)
    for logical, synonyms in COLUMN_SYNONYMS.items():
        if logical in mapping:
            continue
        for syn in synonyms:  # kolejność synonimów = priorytet
            idxs = [i for i, h in enumerate(norm) if h == syn and i not in mapping.values()]
            if idxs:
                mapping[logical] = idxs[0]
                break
    # Kolumny numerowane: "Manufacturer 1" / "MPN 1" jako podstawowe, gdy brak wersji bez numeru
    for logical in ("mpn", "manufacturer"):
        if logical not in mapping:
            for i, h in enumerate(norm):
                m = re.match(r"^(.*?)\s*1$", h)
                if m and _logical_of(m.group(1).strip()) == logical and i not in mapping.values():
                    mapping[logical] = i
                    break
    return mapping


def _alt_pairs(headers: list, mapping: dict[str, int]) -> list[tuple[int, int]]:
    """Pary kolumn zamienników: 'Manufacturer 2'/'MPN 2', 'Alt Manufacturer'/'Alt MPN' itd."""
    groups: dict[str, dict[str, int]] = {}
    used = set(mapping.values())
    for i, h in enumerate(headers):
        if i in used:
            continue
        n = _norm_header(h)
        key = None
        m = re.match(r"^(.*?)\s*(\d+)$", n)
        if m and m.group(2) != "1":
            n, key = m.group(1).strip(), m.group(2)
        if _ALT_WORDS.search(n):
            key = key or "alt"
            n = _ALT_WORDS.sub(" ", n).strip()
        if key is None:
            continue
        logical = _logical_of(n)
        if logical in ("mpn", "manufacturer"):
            groups.setdefault(key, {})[logical] = i
    return [(g["manufacturer"], g["mpn"]) for _, g in sorted(groups.items()) if "manufacturer" in g and "mpn" in g]


def _column_scores(data: list[list], registry: ManufacturerRegistry | None) -> dict[int, dict[str, float]]:
    ncols = max((len(r) for r in data), default=0)
    scores: dict[int, dict[str, float]] = {}
    for c in range(ncols):
        vals = [_cell(r[c]) for r in data if c < len(r) and _cell(r[c])]
        if len(vals) < max(2, len(data) * 0.3):
            continue
        n = len(vals)
        mpn_like = sum(1 for v in vals if (looks_like_mpn(clean_mpn(v).mpn) or is_wildcard(v))
                       and not re.fullmatch(r"\d{1,5}", v))
        refdes = sum(1 for v in vals if _REFDES_RE.match(v))
        qty = sum(1 for v in vals if re.fullmatch(r"\d{1,5}", v))
        mfr = sum(1 for v in vals if registry and registry.resolve(v)[0] is not None) if registry else 0
        uniq = len(set(vals)) / n
        scores[c] = {"mpn": mpn_like / n * (0.5 + 0.5 * uniq), "refdes": refdes / n, "qty": qty / n,
                     "manufacturer": mfr / n}
    return scores


def _best(scores: dict[int, dict[str, float]], key: str, exclude: set[int], threshold: float) -> int | None:
    if key == "mpn":  # kolumna ref. designatorów (R1, U2, "C1,C2") nie jest kolumną MPN
        exclude = exclude | {c for c, v in scores.items() if v["refdes"] >= 0.6}
    cands = [(v[key], c) for c, v in scores.items() if c not in exclude and v[key] >= threshold]
    return max(cands)[1] if cands else None


def _analyse_sheet(name: str, hidden: bool, rows: list[list], explicit: dict[str, str],
                   registry: ManufacturerRegistry | None) -> SheetAnalysis:
    sa = SheetAnalysis(name=name, hidden=hidden)
    nonempty = [i for i, r in enumerate(rows) if any(_cell(c) for c in r)]
    sa.nonempty_rows = len(nonempty)
    if sa.nonempty_rows < 2:
        sa.status = "pusty / brak tabeli"
        return sa

    # 1) nagłówki (pojedynczy wiersz lub dwa wiersze sklejone – nagłówki dwupoziomowe)
    header_idx, mapping = None, {}
    for i in nonempty[:40]:
        candidates = [rows[i]]
        if i + 1 < len(rows):
            nxt = rows[i + 1]
            candidates.append([f"{_cell(a)} {_cell(nxt[j]) if j < len(nxt) else ''}".strip()
                               for j, a in enumerate(rows[i])])
        for k, hdr in enumerate(candidates):
            try:
                mp = _detect_by_headers(hdr, explicit)
            except ValueError:
                continue
            if "mpn" in mp and "manufacturer" in mp:
                header_idx, mapping = (i if k == 0 else i + 1), mp
                sa.headers = [_cell(h) for h in hdr]
                sa.method = "jawnego mapowania" if explicit else "nagłówków"
                break
        if mapping:
            break

    # 2) weryfikacja / wykrywanie po zawartości
    start = (header_idx + 1) if header_idx is not None else None
    if start is None:
        # nagłówek = pierwszy niepusty wiersz, którego komórki nie wyglądają jak dane
        for i in nonempty[:40]:
            texts = [_cell(c) for c in rows[i] if _cell(c)]
            if len(texts) >= 2 and sum(looks_like_mpn(t) for t in texts) <= 1 and \
                    not any(registry and registry.resolve(t)[0] for t in texts):
                start = i + 1
                header_idx = i
                sa.headers = [_cell(h) for h in rows[i]]
                break
        else:
            start = nonempty[0]
            sa.headers = [f"kolumna {j + 1}" for j in range(max(len(r) for r in rows))]
    data = [r for r in rows[start:start + 400] if any(_cell(c) for c in r)]
    scores = _column_scores(data, registry)

    if mapping and not explicit:
        # Nagłówek mógł wskazać złą kolumnę (np. "Part Number" = numer wewnętrzny firmy)
        cur = scores.get(mapping["mpn"], {}).get("mpn", 0)
        alt = _best(scores, "mpn", {mapping["manufacturer"]} | {v for k, v in mapping.items() if k != "mpn"}, 0.6)
        if alt is not None and alt != mapping["mpn"] and cur < 0.3:
            sa.warnings.append(f"kolumna '{sa.headers[mapping['mpn']]}' nie zawiera numerów MPN – użyto "
                               f"'{sa.headers[alt]}' (wykryto po zawartości)")
            mapping["mpn"] = alt
        if registry:
            curm = scores.get(mapping["manufacturer"], {}).get("manufacturer", 0)
            altm = _best(scores, "manufacturer", {mapping["mpn"]}, 0.5)
            if altm is not None and altm != mapping["manufacturer"] and curm < 0.2:
                sa.warnings.append(f"kolumna '{sa.headers[mapping['manufacturer']]}' nie zawiera nazw producentów – "
                                   f"użyto '{sa.headers[altm]}' (wykryto po zawartości)")
                mapping["manufacturer"] = altm
    elif not mapping:
        mfr = _best(scores, "manufacturer", set(), 0.5) if registry else None
        mpn = _best(scores, "mpn", {mfr} if mfr is not None else set(), 0.5)
        if mfr is not None and mpn is not None:
            mapping = {"mpn": mpn, "manufacturer": mfr}
            ref = _best(scores, "refdes", {mpn, mfr}, 0.6)
            if ref is not None:
                mapping["refdes"] = ref
            q = _best(scores, "qty", set(mapping.values()), 0.8)
            if q is not None:
                mapping["quantity"] = q
            sa.method = "zawartości kolumn (nagłówki nierozpoznane – zweryfikuj)"
            sa.warnings.append("kolumny wykryte po zawartości, nie po nagłówkach – zweryfikuj mapowanie "
                               "lub podaj je jawnie (--col)")

    if not mapping:
        sa.status = "brak kolumn MPN / producent"
        return sa
    sa.mapping = mapping
    sa.header_row = (header_idx + 1) if header_idx is not None else None
    sa.alt_pairs = _alt_pairs(sa.headers, mapping)
    first_data = (header_idx + 1) if header_idx is not None else nonempty[0]
    sa.data_rows = sum(1 for r in rows[first_data:]
                       if mapping["mpn"] < len(r) and _cell(r[mapping["mpn"]]))
    sa.status = "zawiera BoM" if sa.data_rows else "nagłówek bez danych"
    return sa


def analyse_workbook(path: str | Path, columns: dict[str, str] | None = None,
                     registry: ManufacturerRegistry | None = None) -> list[tuple[SheetAnalysis, list[list]]]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Nie znaleziono pliku BoM: {path}")
    return [(_analyse_sheet(name, hidden, rows, columns or {}, registry), rows)
            for name, hidden, rows in _load_tables(path)]


def _select_sheets(analysed: list[tuple[SheetAnalysis, list[list]]], sheet: str | None
                   ) -> list[tuple[SheetAnalysis, list[list]]]:
    usable = [(sa, rows) for sa, rows in analysed if sa.mapping and sa.data_rows]
    if sheet:
        if str(sheet).lower() in ("all", "*", "wszystkie"):
            chosen = usable
        else:
            by_name = [(sa, r) for sa, r in analysed if sa.name.lower() == str(sheet).lower()]
            if not by_name and str(sheet).isdigit() and 1 <= int(sheet) <= len(analysed):
                by_name = [analysed[int(sheet) - 1]]
            if not by_name:
                raise ValueError(f"Brak arkusza '{sheet}'. Arkusze: {', '.join(sa.name for sa, _ in analysed)}")
            if not by_name[0][0].mapping:
                raise ValueError(f"Arkusz '{sheet}': {by_name[0][0].status}")
            chosen = by_name
    else:
        # Domyślnie: arkusz z największą liczbą wierszy danych (widoczne mają pierwszeństwo).
        chosen = sorted(usable, key=lambda x: (not x[0].hidden, x[0].data_rows), reverse=True)[:1]
    for sa, _ in analysed:
        if any(sa is c[0] for c in chosen):
            sa.status = "WYBRANY – " + sa.status
        elif sa.mapping and sa.data_rows:
            sa.status = "pominięty (inny arkusz ma więcej danych; użyj --sheet all lub --sheet NAZWA) – " + sa.status
    return chosen


# --------------------------------------------------------------- wiersze

def _aliases_for(registry: ManufacturerRegistry | None, name: str) -> list[str]:
    if not registry:
        return []
    m, _ = registry.resolve(name)
    return [m.name, *m.aliases] if m else []


def _make_row(i: int, sheet: str, mpn_cell, mfr: str, refdes: list[str], qty: str, other_texts: list[str],
              registry, invalid: list[InvalidRow], raw: dict, alternate: bool = False) -> list[BomRow]:
    raw_text = _cell(mpn_cell)
    cleaned = clean_mpn(raw_text, _aliases_for(registry, mfr))
    mpn = cleaned.mpn
    if compact(mpn) == "" or mpn.upper().replace(" ", "") in INVALID_MPN_VALUES:
        if not alternate:
            invalid.append(InvalidRow(i, f"[{sheet}] Brak lub niepoprawny MPN", raw))
        return []
    if not mfr or is_generic_manufacturer(mfr):
        if not alternate:
            invalid.append(InvalidRow(i, f"[{sheet}] Brak konkretnego producenta (MPN {mpn})", raw))
        return []
    if len(mpn) > 64:
        invalid.append(InvalidRow(i, f"[{sheet}] MPN podejrzanie długi (>64 znaki) – prawdopodobnie opis", raw))
        return []
    notes = list(cleaned.notes)
    if isinstance(mpn_cell, (int, float)) and not isinstance(mpn_cell, bool):
        notes.append("MPN zapisany w Excelu jako liczba – sprawdź, czy nie utracono zer wiodących / formatu")
    row = BomRow(i, mpn, mfr, refdes, qty, sheet=sheet, mpn_raw=raw_text, alternate=alternate,
                 wildcard=cleaned.wildcard, hints=hints_from_text(mpn, other_texts), notes=notes)
    out = [row]
    for alt in cleaned.alternates:
        alt_clean = clean_mpn(alt, _aliases_for(registry, mfr))
        if looks_like_mpn(alt_clean.mpn) or alt_clean.wildcard:
            out.append(BomRow(i, alt_clean.mpn, mfr, refdes, qty, sheet=sheet, mpn_raw=raw_text, alternate=True,
                              wildcard=alt_clean.wildcard, notes=["zamiennik z tej samej komórki MPN"]))
    return out


def read_bom(path: str | Path, columns: dict[str, str] | None = None, sheet: str | None = None,
             registry: ManufacturerRegistry | None = None, include_alternates: bool = True
             ) -> tuple[list[BomRow], list[InvalidRow], dict]:
    """Zwraca (poprawne wiersze, niepoprawne wiersze, metadane z analizą arkuszy)."""
    analysed = analyse_workbook(path, columns, registry)
    chosen = _select_sheets(analysed, sheet)
    sheets_info = [sa.describe() for sa, _ in analysed]
    for line in sheets_info:
        log.info("Arkusz %s", line)
    if not chosen:
        detail = "; ".join(sheets_info)
        raise ValueError(
            "Nie znaleziono arkusza z kolumnami MPN i producenta. Analiza arkuszy: " + detail +
            ". Podaj mapowanie, np. --col mpn=\"Nr kat.\" --col manufacturer=\"Producent\" (i ew. --sheet)")

    valid: list[BomRow] = []
    invalid: list[InvalidRow] = []
    empty = 0
    warnings: list[str] = []
    for sa, rows in chosen:
        warnings += [f"[{sa.name}] {w}" for w in sa.warnings]
        mp = sa.mapping
        start = sa.header_row if sa.header_row is not None else 0
        used = set(mp.values()) | {c for pair in sa.alt_pairs for c in pair}

        def cell(row, key):
            idx = mp.get(key)
            return _cell(row[idx]) if idx is not None and idx < len(row) else ""

        for i, row in enumerate(rows[start:], start=start + 1):
            if not any(_cell(c) for c in row):
                empty += 1
                continue
            raw = {sa.headers[j] if j < len(sa.headers) and sa.headers[j] else f"kol{j + 1}": _cell(row[j])
                   for j in range(len(row)) if _cell(row[j])}
            mfr = re.sub(r"\s+", " ", cell(row, "manufacturer"))
            refdes = [r for r in re.split(r"[,;\s]+", cell(row, "refdes")) if r]
            others = [_cell(row[j]) for j in range(len(row)) if j not in used and _cell(row[j])]
            mpn_cell = row[mp["mpn"]] if mp["mpn"] < len(row) else None
            valid += _make_row(i, sa.name, mpn_cell, mfr, refdes, cell(row, "quantity"), others,
                               registry, invalid, raw)
            if include_alternates:
                for mcol, pcol in sa.alt_pairs:
                    amfr = _cell(row[mcol]) if mcol < len(row) else ""
                    acell = row[pcol] if pcol < len(row) else None
                    if _cell(acell):
                        valid += _make_row(i, sa.name, acell, amfr or mfr, refdes, cell(row, "quantity"), others,
                                           registry, invalid, raw, alternate=True)
    first = chosen[0][0]
    meta = {
        "sheet": ", ".join(sa.name for sa, _ in chosen),
        "header_row": first.header_row,
        "columns": {k: first.headers[v] if v < len(first.headers) else f"kol{v + 1}" for k, v in first.mapping.items()},
        "detection": first.method,
        "sheets": sheets_info,
        "warnings": warnings,
        "total_rows": len({(r.sheet, r.row_number) for r in valid}) + len(invalid),
        "empty_rows": empty,
        "alternates": sum(1 for r in valid if r.alternate),
    }
    return valid, invalid, meta


def group_items(rows: list[BomRow], registry: ManufacturerRegistry) -> list[BomItem]:
    """Deduplikacja po (producent, MPN). Ten sam MPN u różnych producentów = różne pozycje."""
    items: dict[tuple[str, str], BomItem] = {}
    multi_sheet = len({r.sheet for r in rows}) > 1
    for r in rows:
        mfr, method = registry.resolve(r.manufacturer)
        mkey = mfr.key if mfr else "?" + strip_legal(normalize_name(r.manufacturer))
        key = (mkey, compact(r.mpn))
        item = items.get(key)
        if item is None:
            item = BomItem(mpn=r.mpn, manufacturer_raw=r.manufacturer, manufacturer=mfr, match_method=method,
                           mpn_bom=r.mpn, alternate=True, wildcard=r.wildcard)
            items[key] = item
        item.alternate = item.alternate and r.alternate
        label = f"{r.sheet}!{r.row_number}" if multi_sheet else str(r.row_number)
        if label not in item.rows:
            item.rows.append(label)
        for d in r.refdes:
            if d not in item.refdes:
                item.refdes.append(d)
        for lst, vals in ((item.manufacturer_variants, [r.manufacturer]), (item.mpn_raw, [r.mpn_raw]),
                          (item.sheets, [r.sheet]), (item.hints, r.hints), (item.mpn_notes, r.notes)):
            for v in vals:
                if v and v not in lst:
                    lst.append(v)
    return list(items.values())

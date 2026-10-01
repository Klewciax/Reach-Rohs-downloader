"""Analiza MPN: czyszczenie wartości z BoM, wykrywanie skrótów / wzorców i rozwinięcie
do pełnego numeru zamówieniowego na podstawie OFICJALNEJ strony producenta.

Typowe przypadki w BoM:
  * pełny numer zamówieniowy:     LM358DR, ATMEGA328P-AU, BAS16,215, LTC3780EG#PBF
  * skrót / nazwa produktu:       LM358, ATMEGA328P, STM32F103C8
  * wzorzec rodziny:              CRCW0603xxxxFKEA, STM32F103C8T*, GRM188R71H…
  * komórka z dodatkami:          "TI LM358DR", "LM358DR (SOIC-8)", "LM358DR\nLM358DT" (zamienniki)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .classify import compact, find_mpn_tokens

log = logging.getLogger(__name__)

# Wzorce rodzin: *, ?, …, "...", ciągi x/X w miejscu znaków (np. 0603xxxxFKEA)
_WILDCARD_RE = re.compile(r"[*?]|\.\.\.|…|(?<=[0-9A-Za-z])[xX]{2,}(?=[0-9A-Za-z]|$)|\[[^\]]*\]")
_MPN_LIKE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/#+,.()]*$")
_SPLIT_ALTERNATES = re.compile(r"\s*(?:\n|;|\||\s+or\s+|\s+lub\s+)\s*", re.I)
# Sufiksy opakowania/wersji oddzielone separatorem – usuwane przy wyszukiwaniu zastępczym.
_PACKAGING_SUFFIXES = [
    (re.compile(r"#.*$"), "sufiks po '#' (np. #PBF, #TR)"),
    (re.compile(r",\d{3}$"), "kod opakowania ',NNN' (Nexperia/NXP)"),
    (re.compile(r"\.\d{3}$"), "kod opakowania '.NNN'"),
    (re.compile(r"/NOPB$", re.I), "sufiks /NOPB"),
    (re.compile(r"[-/](TR|T&R|TAPE|REEL|CT|CUT)$", re.I), "sufiks opakowania (-TR / -CT)"),
    (re.compile(r"\+T?$"), "sufiks '+' / '+T' (lead-free / taśma)"),
]


@dataclass
class CleanedMpn:
    mpn: str
    raw: str
    alternates: list[str] = field(default_factory=list)
    wildcard: bool = False
    notes: list[str] = field(default_factory=list)


def looks_like_mpn(value: str) -> bool:
    v = value.strip()
    if not (3 <= len(v) <= 40) or " " in v:
        return False
    if not _MPN_LIKE.match(v) or not re.search(r"\d", v):
        return False
    return True


def is_wildcard(mpn: str) -> bool:
    return bool(_WILDCARD_RE.search(mpn))


def wildcard_prefix(mpn: str) -> str:
    """Część wzorca przed pierwszym symbolem wieloznacznym (np. 'CRCW0603xxxxFKEA' -> 'CRCW0603')."""
    m = _WILDCARD_RE.search(mpn)
    return mpn[:m.start()].rstrip("-/ ") if m else mpn


def wildcard_regex(mpn: str) -> re.Pattern:
    """'CRCW0603xxxxFKEA' -> ^CRCW0603....FKEA$, 'STM32F103C8T*' -> ^STM32F103C8T.*$"""
    out, i = [], 0
    while i < len(mpn):
        m = _WILDCARD_RE.match(mpn, i)
        if m:
            tok = m.group(0)
            if tok in ("*", "...", "…"):
                out.append(".*")
            elif tok == "?":
                out.append(".")
            elif tok.startswith("["):
                out.append(".")
            else:
                out.append("." * len(tok))
            i = m.end()
        else:
            out.append(re.escape(mpn[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.I)


def clean_mpn(raw: str, manufacturer_aliases: list[str] | None = None) -> CleanedMpn:
    """Wyciąga MPN z komórki BoM i zapisuje, co zostało zmienione."""
    text = str(raw or "").replace(" ", " ").replace("–", "-").replace("‑", "-").strip()
    res = CleanedMpn(mpn=text, raw=str(raw or ""))
    if not text:
        return res
    parts = [p for p in _SPLIT_ALTERNATES.split(text) if p.strip()]
    if len(parts) > 1:
        text = parts[0]
        res.alternates = [p.strip() for p in parts[1:]]
        res.notes.append(f"komórka zawiera kilka numerów – użyto pierwszego, pozostałe jako zamienniki: "
                         f"{', '.join(res.alternates)}")
    # Prefiks z nazwą producenta: "TI LM358DR", "Murata: GRM188..."
    for alias in sorted(manufacturer_aliases or [], key=len, reverse=True):
        m = re.match(rf"^{re.escape(alias)}\s*[:\-]?\s+(.+)$", text, re.I)
        if m and looks_like_mpn(m.group(1).split()[0]):
            text = m.group(1)
            res.notes.append(f"usunięto prefiks producenta '{alias}'")
            break
    # Opis w nawiasie lub po myślniku otoczonym spacjami: "LM358DR (SOIC-8)", "LM358DR - opamp"
    stripped = re.sub(r"\s*\([^)]*\)\s*$", "", text)
    stripped = re.split(r"\s+[-–]\s+", stripped)[0].strip()
    if " " in stripped:
        tokens = stripped.split()
        if looks_like_mpn(tokens[0]) or is_wildcard(tokens[0]):
            stripped = tokens[0]
    if stripped != text:
        res.notes.append(f"z komórki usunięto dodatkowy tekst: '{text}' -> '{stripped}'")
        text = stripped
    res.mpn = text
    if is_wildcard(text):
        res.wildcard = True
        res.notes.append("MPN jest wzorcem rodziny (symbole wieloznaczne) – nie wskazuje konkretnej części")
    return res


def hints_from_text(mpn: str, texts: list[str]) -> list[str]:
    """Pełne numery w innych kolumnach wiersza (np. opis), zaczynające się od MPN z BoM."""
    out: list[str] = []
    for t in texts:
        if not t:
            continue
        _, longer = find_mpn_tokens(str(t), mpn)
        for tok in longer:
            if looks_like_mpn(tok) and tok not in out:
                out.append(tok)
    return out


def packaging_trims(mpn: str) -> list[tuple[str, str]]:
    """Krótsze formy MPN bez sufiksów opakowania (do wyszukiwania zastępczego)."""
    out: list[tuple[str, str]] = []
    for rx, why in _PACKAGING_SUFFIXES:
        trimmed = rx.sub("", mpn).strip()
        if trimmed and trimmed != mpn and len(compact(trimmed)) >= 4 and trimmed not in (t for t, _ in out):
            out.append((trimmed, why))
    return out


# ------------------------------------------------------------ sprawdzenie online

@dataclass
class MpnCheck:
    form: str = "nie sprawdzono"   # pełny / skrócony / wzorzec / nie znaleziono / nie sprawdzono
    variants: list[str] = field(default_factory=list)
    expanded_to: str = ""
    source_url: str = ""
    notes: list[str] = field(default_factory=list)


FORM_FULL = "pełny (potwierdzony na stronie producenta)"
FORM_ABBREV = "skrócony (nazwa produktu / rodziny)"
FORM_WILDCARD = "wzorzec rodziny"
FORM_NOT_FOUND = "nie znaleziono na stronie producenta"
FORM_UNCHECKED = "nie sprawdzono (brak strony produktu w konfiguracji)"


def inspect_mpn(item, adapter, ctx, expand: bool = True) -> MpnCheck:
    """Sprawdza na stronie produktu producenta, czy MPN z BoM jest pełnym numerem
    zamówieniowym, czy skrótem, i ewentualnie rozwija go do pełnej postaci."""
    from .models import SearchResult

    check = MpnCheck()
    mpn = item.mpn
    if item.wildcard:
        check.form = FORM_WILDCARD
        query = wildcard_prefix(mpn)
    else:
        query = mpn
    pages = adapter.product_pages_detailed(query, ctx)
    if not pages:
        check.form = FORM_WILDCARD if item.wildcard else FORM_UNCHECKED
        return check
    base_equal = compact(adapter.base_part(query)) == compact(query)
    seen_any_page = False
    for url, uses_base in pages:
        page = adapter.fetch_html(url, ctx, SearchResult())
        if page is None:
            continue
        seen_any_page = True
        soup, final = page
        text = soup.get_text(" ")
        exact, longer = find_mpn_tokens(text, query)
        longer = [v for v in longer if re.search(r"\d", v)][:20]
        if not exact and not longer:
            continue
        check.source_url = final
        check.variants = longer
        if item.wildcard:
            rx = wildcard_regex(mpn)
            check.variants = [v for v in longer if rx.match(v)] or longer
        elif exact and not (uses_base and base_equal and longer):
            # Strona konkretnego numeru zamówieniowego (lub brak wariantów) i dokładne trafienie = pełny MPN.
            # Na stronie produktu bazowego ({base}) MPN równy nazwie produktu z wariantami = skrót.
            check.form = FORM_FULL
            return check
        else:
            check.form = FORM_ABBREV
        break
    else:
        if seen_any_page:
            check.form = FORM_NOT_FOUND
            check.notes.append("MPN nie występuje na stronie produktu producenta – sprawdź poprawność numeru w BoM")
        else:
            check.notes.append("strona produktu niedostępna – formy MPN nie zweryfikowano")
        if item.wildcard:
            check.form = FORM_WILDCARD
        return check

    # Skrót lub wzorzec: wybór pełnego numeru
    confirmed_hints = [h for h in item.hints if compact(h) in {compact(v) for v in check.variants}]
    if confirmed_hints:
        choice, why = confirmed_hints[0], "pełny numer z opisu w BoM potwierdzony na stronie producenta"
    elif len(check.variants) == 1:
        choice, why = check.variants[0], "jedyny wariant zamówieniowy na stronie producenta"
    else:
        choice, why = "", ""
    if choice and expand:
        check.expanded_to = choice
        check.notes.append(f"MPN rozwinięty automatycznie: {mpn} -> {choice} ({why}) – zweryfikuj")
    elif check.variants:
        check.notes.append(f"MPN niejednoznaczny – na stronie producenta jest {len(check.variants)} wariantów: "
                           f"{', '.join(check.variants[:10])}{' …' if len(check.variants) > 10 else ''}. "
                           "Uzupełnij pełny numer w BoM")
    return check

"""Smoke test narzędzia.

    python -m bom_compliance.smoke            # offline: konfiguracja, rejestr, adaptery, przykładowy BoM
    python -m bom_compliance.smoke --live     # + prawdziwe zapytania do stron producentów

Tryb offline nie wykonuje żadnych zapytań sieciowych i nadaje się do CI.
Tryb live sprawdza, czy skonfigurowane URL-e producentów nadal działają (serwisy się
zmieniają) i czy adaptery znajdują dokumenty dla znanych części (config/smoke_parts.yaml).

Kody wyjścia: 0 = OK, 1 = błąd konfiguracji lub zepsuty URL (404/410, przekierowanie poza
domenę), 2 = tylko problemy z siecią (nic nie dało się sprawdzić / host nieosiągalny).
"""
from __future__ import annotations

import argparse
import logging
import string
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from .adapters import ADAPTERS, AdapterContext, get_adapter
from .bom import group_items, read_bom
from .config import DEFAULT_CONFIG, DEFAULT_MANUFACTURERS, PACKAGE_ROOT, Settings
from .http_client import DomainNotAllowed, FetchError, LoginRequired, PoliteSession, RobotsDisallowed, host_allowed
from .manufacturers import ManufacturerRegistry
from .models import BomItem

EXAMPLE_BOM = PACKAGE_ROOT / "examples" / "bom_example.csv"
EXAMPLE_XLSX = PACKAGE_ROOT / "examples" / "bom_example_multisheet.xlsx"
SMOKE_PARTS = PACKAGE_ROOT / "config" / "smoke_parts.yaml"
_TEMPLATE_FIELDS = {"mpn", "mpn_lower", "base", "base_lower"}

OK, WARN, BROKEN, NET = "OK", "WARN", "BROKEN", "NETWORK"


@dataclass
class Check:
    level: str
    area: str
    message: str


def _static_urls(m) -> list[tuple[str, str]]:
    out = [("compliance_pages", u) for u in m.compliance_pages]
    out += [("contact_pages", u) for u in m.contact_pages]
    out += [("longevity_pages", u) for u in m.longevity_pages]
    out += [("general_documents", d["url"]) for d in m.general_documents]
    out += [("longevity_documents", d["url"]) for d in m.longevity_documents]
    return out


def offline_checks(manufacturers_file: str | Path = DEFAULT_MANUFACTURERS) -> list[Check]:
    checks: list[Check] = []
    try:
        Settings.load(None)
        checks.append(Check(OK, "config", f"{DEFAULT_CONFIG.name} wczytany"))
    except Exception as exc:
        return [Check(BROKEN, "config", f"Nie można wczytać konfiguracji: {exc}")]
    try:
        reg = ManufacturerRegistry.from_yaml(manufacturers_file)
        checks.append(Check(OK, "registry", f"{len(reg.manufacturers)} producentów, aliasy bez konfliktów"))
    except Exception as exc:
        return checks + [Check(BROKEN, "registry", f"Błąd rejestru producentów: {exc}")]

    for key, m in reg.manufacturers.items():
        if m.adapter not in ADAPTERS:
            checks.append(Check(BROKEN, key, f"nieznany adapter '{m.adapter}'"))
        # Każdy skonfigurowany URL musi leżeć na domenie producenta – gwarancja pochodzenia plików.
        for field_name, url in _static_urls(m):
            if not host_allowed(url, m.domains):
                checks.append(Check(BROKEN, key, f"{field_name}: URL spoza domen producenta {m.domains}: {url}"))
        for tpl in m.product_pages + m.search_urls:
            names = {f for _, f, _, _ in string.Formatter().parse(tpl) if f}
            if names - _TEMPLATE_FIELDS:
                checks.append(Check(BROKEN, key, f"nieznane pola {names - _TEMPLATE_FIELDS} w szablonie {tpl}"))
            elif not host_allowed(tpl.format(mpn="X", mpn_lower="x", base="X", base_lower="x"), m.domains):
                checks.append(Check(BROKEN, key, f"szablon spoza domen producenta: {tpl}"))
        for d in m.general_documents + m.longevity_documents:
            if "url" not in d:
                checks.append(Check(BROKEN, key, f"dokument bez pola url: {d}"))
    if not any(c.level == BROKEN for c in checks):
        checks.append(Check(OK, "registry", "wszystkie URL-e i szablony leżą na domenach producentów"))

    # Adaptery: każdy da się utworzyć i ma unikalny klucz
    for name in ADAPTERS:
        try:
            get_adapter(name)
        except Exception as exc:  # pragma: no cover
            checks.append(Check(BROKEN, "adapters", f"{name}: {exc}"))
    checks.append(Check(OK, "adapters", ", ".join(sorted(ADAPTERS))))

    # Przykładowy BoM: parsowanie + deduplikacja
    try:
        rows, invalid, meta = read_bom(EXAMPLE_BOM, registry=reg)
        items = group_items(rows, reg)
        checks.append(Check(OK, "bom", f"przykładowy BoM: {len(rows)} wierszy, {len(invalid)} niepoprawnych, "
                                      f"{len(items)} pozycji"))
    except Exception as exc:
        checks.append(Check(BROKEN, "bom", f"przykładowy BoM: {exc}"))
    # BoM wielokartowy (pusta okładka, lista na 2. karcie, polskie nagłówki, zamienniki, skróty MPN)
    if EXAMPLE_XLSX.is_file():
        try:
            rows, invalid, meta = read_bom(EXAMPLE_XLSX, registry=reg)
            ok = meta["sheet"] == "BOM" and meta.get("alternates", 0) > 0
            checks.append(Check(OK if ok else BROKEN, "bom",
                                f"BoM wielokartowy: wybrano arkusz '{meta['sheet']}', {len(rows)} wierszy "
                                f"(zamienników: {meta.get('alternates', 0)})"))
        except Exception as exc:
            checks.append(Check(BROKEN, "bom", f"BoM wielokartowy: {exc}"))
    return checks


def _probe(session: PoliteSession, url: str, domains: list[str]) -> Check:
    try:
        resp = session.get(url, domains, stream=True)
        ctype = resp.headers.get("Content-Type", "")
        resp.close()
        return Check(OK, "", f"200 {ctype.split(';')[0]} {url}")
    except LoginRequired as exc:
        return Check(WARN, "", f"wymaga logowania: {exc.url}")
    except RobotsDisallowed:
        return Check(WARN, "", f"zablokowane przez robots.txt: {url}")
    except DomainNotAllowed as exc:
        return Check(BROKEN, "", f"przekierowanie poza domenę producenta ({exc.url}): {url}")
    except FetchError as exc:
        if exc.status in (404, 410):
            return Check(BROKEN, "", f"HTTP {exc.status} – URL nieaktualny: {url}")
        if exc.status is not None:
            return Check(WARN, "", f"HTTP {exc.status}: {url}")
        return Check(NET, "", f"{exc}")


def live_checks(settings: Settings, only: set[str] | None = None, parts_file: Path = SMOKE_PARTS) -> list[Check]:
    from .pipeline import Pipeline

    reg = ManufacturerRegistry.from_yaml(settings.manufacturers_file)
    parts = (yaml.safe_load(parts_file.read_text(encoding="utf-8")) or {}).get("parts", {})
    session = PoliteSession(settings)
    checks: list[Check] = []
    with tempfile.TemporaryDirectory() as tmp:
        pipeline = Pipeline(settings, Path(tmp), session, registry=reg)
        # API dystrybutorów (jeśli podano klucze)
        for missing in pipeline.hub.missing_keys:
            checks.append(Check(WARN, "dystrybutor", f"pominięty – brak klucza API: {missing}"))
        for client in pipeline.hub.clients:
            try:
                parts = client.lookup("LM358DR")
                checks.append(Check(OK if parts else WARN, "dystrybutor",
                                    f"{client.name}: API odpowiada, wyników dla LM358DR: {len(parts)}"))
            except LoginRequired as exc:
                checks.append(Check(BROKEN, "dystrybutor", f"{client.name}: klucz API odrzucony – {exc} "
                                                          "(popraw config/credentials.yaml)"))
            except FetchError as exc:
                checks.append(Check(NET if exc.status is None else WARN, "dystrybutor", f"{client.name}: {exc}"))
        for key, m in reg.manufacturers.items():
            if only and key not in only:
                continue
            for _field, url in _static_urls(m):
                c = _probe(session, url, m.domains)
                checks.append(Check(c.level, key, c.message))
            mpn = parts.get(key)
            if not mpn:
                continue
            item = BomItem(mpn=mpn, manufacturer_raw=m.name, manufacturer=m, match_method="exact")
            res = pipeline.process_item(item)
            network_only = bool(res.reasons) and all("Błąd sieci" in r or "błąd sieci" in r
                                                     for r in res.reasons if r.startswith("Błędy"))
            level = OK if res.docs else (NET if network_only and any(r.startswith("Błędy") for r in res.reasons)
                                         else WARN)
            msg = f"{mpn}: RoHS={res.rohs_status.value} REACH={res.reach_status.value}, plików: {len(res.docs)}"
            if res.lifecycle:
                msg += f", cykl życia={res.lifecycle.status.value}"
            if res.longevity:
                msg += f", longevity={'tak' if res.longevity.found else 'nie'}"
            if res.reasons:
                msg += " | " + " | ".join(res.reasons)[:300]
            checks.append(Check(level, key, msg))
            # wzorce stron produktu (cykl życia)
            ctx = AdapterContext(session, settings, m)
            for url in get_adapter(m.adapter).product_page_urls(mpn, ctx):
                c = _probe(session, url, m.domains)
                checks.append(Check(c.level, key, "product_page: " + c.message))
    return checks


def print_checks(checks: list[Check]) -> None:
    icon = {OK: "  OK  ", WARN: " WARN ", BROKEN: "BROKEN", NET: " NET  "}
    for c in checks:
        print(f"[{icon[c.level]}] {c.area:<20} {c.message}")


def exit_code(checks: list[Check], live: list[Check] | None = None) -> int:
    if any(c.level == BROKEN for c in checks + (live or [])):
        return 1
    if live and any(c.level == NET for c in live) and not any(c.level == OK for c in live):
        return 2  # nic nie udało się sprawdzić – problem z siecią, nie z narzędziem
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bom_compliance.smoke", description="Smoke test bom-compliance")
    p.add_argument("--live", action="store_true", help="Wykonaj prawdziwe zapytania do stron producentów")
    p.add_argument("--only", action="append", default=[], help="Klucz producenta z manufacturers.yaml (wielokrotnie)")
    p.add_argument("-c", "--config", help="Plik konfiguracyjny YAML")
    p.add_argument("--lifecycle", action="store_true", help="W trybie live sprawdź też cykl życia")
    p.add_argument("--longevity", action="store_true", help="W trybie live sprawdź też longevity")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    checks = offline_checks()
    print("== Smoke test: offline ==")
    print_checks(checks)
    live: list[Check] = []
    if args.live:
        settings = Settings.load(args.config, {"check_lifecycle": args.lifecycle or None,
                                               "check_longevity": args.longevity or None,
                                               "save_lifecycle_snapshots": False})
        print("\n== Smoke test: live (prawdziwe zapytania, z zachowaniem limitów tempa) ==")
        live = live_checks(settings, set(args.only) or None)
        print_checks(live)
    code = exit_code(checks, live)
    print(f"\nWynik: {'OK' if code == 0 else 'BŁĘDY KONFIGURACJI / NIEAKTUALNE URL-E' if code == 1 else 'PROBLEMY Z SIECIĄ'}"
          f" (kod {code})")
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

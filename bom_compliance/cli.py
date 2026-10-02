"""Interfejs wiersza poleceń."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .adapters import AdapterContext
from .bom import group_items, read_bom
from .config import Settings
from .contacts import find_contacts
from .manufacturers import ManufacturerRegistry
from .pipeline import Pipeline
from .report import build_request_groups, missing_types, print_console_summary, summarize, write_reports


def _parse_cols(values: list[str]) -> dict[str, str]:
    out = {}
    for v in values or []:
        if "=" not in v:
            raise argparse.ArgumentTypeError(f"--col oczekuje formatu nazwa=Kolumna, otrzymano: {v}")
        k, col = v.split("=", 1)
        k = k.strip().lower()
        if k not in ("mpn", "manufacturer", "refdes", "quantity"):
            raise argparse.ArgumentTypeError(f"Nieznana kolumna logiczna '{k}' (mpn, manufacturer, refdes, quantity)")
        out[k] = col.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bom-compliance",
        description="Pobiera deklaracje RoHS/REACH wyłącznie z oficjalnych stron producentów na podstawie BoM.",
    )
    p.add_argument("bom", help="Plik BoM (.csv, .tsv, .xlsx)")
    p.add_argument("-o", "--output", help="Katalog wyjściowy (domyślnie: output_dir z konfiguracji)")
    p.add_argument("-c", "--config", help="Plik YAML z konfiguracją (nadpisuje config/default.yaml)")
    p.add_argument("-m", "--manufacturers", help="Plik YAML z rejestrem producentów")
    p.add_argument("--col", action="append", default=[], metavar="NAZWA=KOLUMNA",
                   help="Mapowanie kolumn, np. --col mpn=\"Mfr Part #\" --col manufacturer=Mfr (wielokrotnie)")
    p.add_argument("--sheet", help="Arkusz Excela: nazwa, numer (od 1) albo 'all' (połącz wszystkie arkusze z BoM). "
                                   "Domyślnie: arkusz z największą liczbą wierszy BoM")
    p.add_argument("--no-alternates", action="store_true", help="Pomiń zamienniki (Manufacturer 2 / MPN 2 itp.)")
    p.add_argument("--no-mpn-check", action="store_true",
                   help="Nie sprawdzaj na stronie producenta, czy MPN jest pełny czy skrócony")
    p.add_argument("--delay", type=float, help="Minimalny odstęp między zapytaniami do hosta [s]")
    p.add_argument("--timeout", type=float, help="Timeout odczytu [s]")
    p.add_argument("--retries", type=int, help="Liczba ponowień przy 429/5xx/błędach sieci")
    p.add_argument("--only", action="append", default=[], metavar="PRODUCENT",
                   help="Przetwarzaj tylko wskazanych producentów (nazwa/alias; wielokrotnie)")
    p.add_argument("--no-general", action="store_true", help="Nie pobieraj ogólnych oświadczeń producentów")
    p.add_argument("--no-contacts", action="store_true", help="Nie wyszukuj kontaktów na stronach producentów")
    p.add_argument("--lifecycle", action="store_true",
                   help="Status cyklu życia (Active / NRND / Last Time Buy / EOL) – domyślnie włączony")
    p.add_argument("--longevity", action="store_true",
                   help="Długość produkcji / programy longevity producenta – domyślnie włączone")
    p.add_argument("--no-lifecycle", action="store_true", help="Nie sprawdzaj statusu cyklu życia")
    p.add_argument("--no-longevity", action="store_true", help="Nie sprawdzaj długości produkcji (longevity)")
    p.add_argument("--credentials", metavar="PLIK",
                   help="Plik z kluczami API dystrybutorów (domyślnie config/credentials.yaml)")
    p.add_argument("--manufacturer-only", action="store_true",
                   help="Tylko strony producentów – bez zapasowego źródła u dystrybutorów (Octopart/DigiKey/Mouser/TME)")
    p.add_argument("--no-discovery", action="store_true",
                   help="Nie wykrywaj automatycznie stron producentów spoza rejestru")
    p.add_argument("--dry-run", action="store_true", help="Tylko wczytaj i zdeduplikuj BoM, bez zapytań sieciowych")
    p.add_argument("-v", "--verbose", action="count", default=0, help="Więcej logów (-v, -vv)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cols = _parse_cols(args.col)
    except argparse.ArgumentTypeError as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2
    overrides = {
        "output_dir": args.output, "manufacturers_file": args.manufacturers, "columns": cols or None,
        "sheet": args.sheet, "min_delay_per_host": args.delay, "read_timeout": args.timeout,
        "max_retries": args.retries, "download_general_statements": False if args.no_general else None,
        "check_lifecycle": False if args.no_lifecycle else (True if args.lifecycle else None),
        "include_alternates": False if args.no_alternates else None,
        "distributor_fallback": False if args.manufacturer_only else None,
        "credentials_file": args.credentials,
        "auto_discover_manufacturers": False if args.no_discovery else None,
        "inspect_mpn": False if args.no_mpn_check else None,
        "check_longevity": False if args.no_longevity else (True if args.longevity else None),
    }
    try:
        settings = Settings.load(args.config, overrides)
    except (OSError, ValueError) as exc:
        print(f"Błąd konfiguracji: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(settings.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    level = logging.WARNING if args.verbose == 0 else logging.INFO if args.verbose == 1 else logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for h in logging.getLogger().handlers:
        h.setLevel(level)
    fh = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    logging.getLogger().addHandler(fh)
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log = logging.getLogger("bom_compliance")

    try:
        registry = ManufacturerRegistry.from_yaml(settings.manufacturers_file, settings.fuzzy_manufacturer_match,
                                                  settings.fuzzy_cutoff)
        if settings.auto_discover_manufacturers:
            n_disc = registry.merge_yaml(settings.discovered_manufacturers_file)
            if n_disc:
                log.info("Wczytano %d producentów wykrytych automatycznie wcześniej (%s)", n_disc,
                         settings.discovered_manufacturers_file)
        rows, invalid, meta = read_bom(args.bom, settings.columns, settings.sheet, registry,
                                       settings.include_alternates)
    except (OSError, ValueError) as exc:
        print(f"Błąd: {exc}", file=sys.stderr)
        return 2

    items = group_items(rows, registry)
    if args.only:
        wanted = set()
        for name in args.only:
            m, _ = registry.resolve(name)
            if m is None:
                print(f"Błąd: nieznany producent w --only: {name}", file=sys.stderr)
                return 2
            wanted.add(m.key)
        items = [i for i in items if i.manufacturer and i.manufacturer.key in wanted]
    log.info("BoM: %d poprawnych wierszy, %d niepoprawnych, %d unikalnych pozycji", len(rows), len(invalid), len(items))
    print("Analiza arkuszy BoM:")
    for line in meta.get("sheets", []):
        print(f"  - {line}")
    for w in meta.get("warnings", []):
        print(f"  UWAGA: {w}")
    print(f"BoM: {len(rows)} poprawnych wierszy (w tym zamienników: {meta.get('alternates', 0)}), "
          f"{len(invalid)} niepoprawnych, {len(items)} unikalnych pozycji (producent + MPN).")
    if args.dry_run:
        for i in items:
            flags = []
            if i.alternate:
                flags.append("zamiennik")
            if i.wildcard:
                flags.append("wzorzec")
            if i.hints:
                flags.append("w opisie: " + ",".join(i.hints))
            print(f"  {i.manufacturer_name:<24} {i.mpn:<24} dopasowanie={i.match_method:<8} "
                  f"adapter={i.manufacturer.adapter if i.manufacturer else '-':<10} refdes={','.join(i.refdes)}"
                  + (f"  [{'; '.join(flags)}]" if flags else ""))
            for n in i.mpn_notes:
                print(f"      · {n}")
        for r in invalid:
            print(f"  ✗ wiersz {r.row_number}: {r.reason}")
        return 0

    pipeline = Pipeline(settings, out_dir, registry=registry)
    if settings.distributor_fallback or settings.auto_discover_manufacturers:
        if pipeline.hub.active:
            print("Dystrybutorzy (zapasowe źródło, API): " + ", ".join(c.name for c in pipeline.hub.clients))
        if pipeline.hub.missing_keys:
            print("Dystrybutorzy pominięci – brak kluczy API: " + "; ".join(pipeline.hub.missing_keys))
            if not Path(settings.credentials_file).is_file():
                print("  Aby ich użyć: python -m bom_compliance.credentials init  (i wpisz klucze w "
                      "config/credentials.yaml)")

    def progress(n, total, res):
        shown = res.item.mpn_bom if res.item.mpn_bom == res.item.mpn else f"{res.item.mpn_bom} -> {res.item.mpn}"
        print(f"[{n}/{total}] {res.item.manufacturer_name} {shown}: "
              f"RoHS={res.rohs_status.value} REACH={res.reach_status.value}", flush=True)

    try:
        results = pipeline.process(items, progress)
    except KeyboardInterrupt:
        print("Przerwano.", file=sys.stderr)
        return 130

    pipeline.discovery.save(out_dir / "discovered_manufacturers.yaml")
    for name, err in pipeline.hub.errors.items():
        print(f"UWAGA: {name}: {err}", file=sys.stderr)

    contacts = {}
    if not args.no_contacts:
        need = {r.item.manufacturer.key: r.item.manufacturer for r in results
                if r.item.manufacturer and missing_types(r, settings)}
        for key, m in need.items():
            log.info("Szukam kontaktu compliance: %s", m.name)
            contacts[key] = find_contacts(m, AdapterContext(pipeline.session, settings, m))

    summary = summarize(results, settings, meta, invalid)
    groups = build_request_groups(results, settings, contacts)
    paths = write_reports(out_dir, results, invalid, meta, settings, groups, summary)
    paths["log"] = out_dir / "run.log"
    print_console_summary(summary, groups, paths, results)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

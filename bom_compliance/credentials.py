"""Dane logowania do API dystrybutorów (config/credentials.yaml).

    python -m bom_compliance.credentials                  # KREATOR: wybierz serwisy i wpisz klucze (ukryte)
    python -m bom_compliance.credentials status           # które klucze są ustawione (zamaskowane)
    python -m bom_compliance.credentials status --check   # dodatkowo sprawdź klucze zapytaniem do API
    python -m bom_compliance.credentials remove digikey   # usuń klucze serwisu
    python -m bom_compliance.credentials init             # pusty plik ze wzoru (do ręcznej edycji)

Kolejność (ostatnie wygrywa): plik credentials.yaml < `api_keys:` w konfiguracji < zmienne środowiskowe.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import PACKAGE_ROOT

log = logging.getLogger(__name__)

DEFAULT_CREDENTIALS = PACKAGE_ROOT / "config" / "credentials.yaml"
EXAMPLE_CREDENTIALS = PACKAGE_ROOT / "config" / "credentials.example.yaml"

# sekcja.pole w YAML -> nazwa zmiennej środowiskowej używanej przez klientów API
FIELDS = {
    ("digikey", "client_id"): "DIGIKEY_CLIENT_ID",
    ("digikey", "client_secret"): "DIGIKEY_CLIENT_SECRET",
    ("nexar", "client_id"): "NEXAR_CLIENT_ID",
    ("nexar", "client_secret"): "NEXAR_CLIENT_SECRET",
    ("mouser", "api_key"): "MOUSER_API_KEY",
    ("tme", "token"): "TME_TOKEN",
    ("tme", "app_secret"): "TME_APP_SECRET",
}


def load_credentials(path: str | Path | None = None) -> dict[str, str]:
    """Wczytuje plik z kluczami i zwraca słownik {NAZWA_ZMIENNEJ: wartość} (tylko niepuste)."""
    path = Path(path) if path else DEFAULT_CREDENTIALS
    if not path.is_file():
        return {}
    _warn_permissions(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Błąd składni w pliku z kluczami {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError(f"Plik z kluczami {path} musi zawierać sekcje digikey / nexar / mouser / tme")
    out: dict[str, str] = {}
    unknown = set(data) - {s for s, _ in FIELDS}
    if unknown:
        log.warning("Nieznane sekcje w %s: %s", path, ", ".join(sorted(unknown)))
    for (section, key), env in FIELDS.items():
        value = (data.get(section) or {}).get(key)
        if value not in (None, "") and str(value).strip():
            out[env] = str(value).strip()
    return out


def _warn_permissions(path: Path) -> None:
    if os.name != "posix":
        return
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        log.warning("Plik z kluczami %s jest czytelny dla innych użytkowników – zalecane: chmod 600 %s", path, path)


def merged_keys(settings) -> dict[str, str]:
    """Plik credentials < api_keys z konfiguracji (zmienne środowiskowe sprawdza sam klient API)."""
    keys = load_credentials(getattr(settings, "credentials_file", None))
    keys.update({str(k).upper(): str(v) for k, v in (settings.api_keys or {}).items() if v})
    return keys


def _mask(v: str) -> str:
    return v[:3] + "…" + v[-2:] if len(v) > 8 else "***"


def status_lines(settings, check: bool = False) -> list[str]:
    from .distributors import CLIENTS, DistributorHub
    from .http_client import PoliteSession

    keys = merged_keys(settings)
    lines = []
    path = Path(settings.credentials_file)
    lines.append(f"Plik z kluczami: {path} ({'jest' if path.is_file() else 'BRAK – uruchom kreator: python -m bom_compliance.credentials'})")
    seen = set()
    for name, cls in CLIENTS.items():
        if cls in seen:
            continue
        seen.add(cls)
        parts = []
        for env in cls.env:
            if os.environ.get(env):
                parts.append(f"{env}={_mask(os.environ[env])} (zmienna środowiskowa)")
            elif keys.get(env):
                parts.append(f"{env}={_mask(keys[env])} (plik)")
            else:
                parts.append(f"{env}=brak")
        ok = all(os.environ.get(e) or keys.get(e) for e in cls.env)
        lines.append(f"  [{'OK ' if ok else ' - '}] {cls.name:<15} " + ", ".join(parts))
    if check:
        hub = DistributorHub(PoliteSession(settings), settings.distributor_sources, keys)
        for client in hub.clients:
            try:
                parts = client.lookup("LM358DR")
                lines.append(f"  test {client.name}: API odpowiada (wyników dla LM358DR: {len(parts)})")
            except Exception as exc:  # noqa: BLE001 – pokazujemy każdy błąd użytkownikowi
                lines.append(f"  test {client.name}: BŁĄD – {exc}")
    return lines


# ---------------------------------------------------------------- kreator

@dataclass
class Service:
    section: str
    name: str
    rating: str
    gives: str
    signup: list[str]
    fields: list[tuple[str, str]]  # (pole w YAML, opis pola)
    client: str                    # klucz w distributors.CLIENTS


SERVICES = [
    Service("digikey", "DigiKey", "★★★ zalecane",
            "certyfikaty RoHS/REACH producentów, status RoHS / REACH / produktu",
            ["Wejdź na https://developer.digikey.com/ i zaloguj się kontem DigiKey.",
             "Utwórz organizację, a w niej aplikację produkcyjną z dostępem do API 'Product Information v4'.",
             "Skopiuj z aplikacji Client ID i Client Secret."],
            [("client_id", "Client ID"), ("client_secret", "Client Secret")], "digikey"),
    Service("nexar", "Nexar / Octopart", "★★★ zalecane",
            "oficjalna strona producenta (wykrywanie producentów spoza listy), dokumenty części",
            ["Wejdź na https://portal.nexar.com/ i załóż konto.",
             "Kliknij 'Create app', nadaj aplikacji dostęp do zakresu 'Supply'.",
             "W zakładce 'Authorization' aplikacji skopiuj Client ID i Client Secret."],
            [("client_id", "Client ID"), ("client_secret", "Client Secret")], "nexar"),
    Service("mouser", "Mouser", "★ pomocniczo",
            "status RoHS i cyklu życia (bez plików z deklaracjami)",
            ["Wejdź na https://www.mouser.com/en/api-search/ i zarejestruj się do 'Search API'.",
             "Klucz API przyjdzie e-mailem."],
            [("api_key", "API Key")], "mouser"),
    Service("tme", "TME", "eksperymentalne",
            "dokumenty produktu; korzysta ze starszej wersji TME API (może przestać działać)",
            ["Na tme.eu w panelu klienta wejdź w 'Aplikacje' i zarejestruj aplikację (dostaniesz token tymczasowy).",
             "Na https://developers.tme.eu/ utwórz aplikację, podaj token tymczasowy.",
             "Skopiuj token (50 znaków) i sekret aplikacji (20 znaków)."],
            [("token", "Token"), ("app_secret", "App secret")], "tme"),
]


def _read_raw(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def write_credentials(path: Path, values: dict[str, dict[str, str]]) -> None:
    """Zapisuje klucze, zachowując komentarze/instrukcje ze wzoru (credentials.example.yaml)."""
    text = EXAMPLE_CREDENTIALS.read_text(encoding="utf-8")
    for section, fields in values.items():
        for field_name, value in fields.items():
            pattern = re.compile(rf"(^{re.escape(section)}:\n(?:[ \t].*\n|#.*\n|\n)*?[ \t]+{re.escape(field_name)}:)[^\n]*",
                                 re.M)
            text, n = pattern.subn(lambda m: m.group(1) + " " + json.dumps(value or "", ensure_ascii=False), text, 1)
            if not n:
                raise ValueError(f"Wzór nie zawiera pola {section}.{field_name}")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    if os.name == "posix":
        tmp.chmod(0o600)
    tmp.replace(path)


def _parse_choice(answer: str, n: int) -> list[int] | None:
    answer = answer.strip().lower()
    if answer in ("", "q", "0"):
        return []
    if answer in ("all", "wszystkie", "a", "*"):
        return list(range(n))
    out = []
    for part in re.split(r"[,\s]+", answer):
        if not part.isdigit() or not 1 <= int(part) <= n:
            return None
        if int(part) - 1 not in out:
            out.append(int(part) - 1)
    return out


def setup_wizard(path: Path, input_fn=input, secret_fn=getpass.getpass, out=print, test_fn=None) -> int:
    """Interaktywny wybór serwisów i wpisanie kluczy (ukryte znaki). Zwraca liczbę zmienionych serwisów."""
    current = _read_raw(path)
    values = {s.section: {f: str((current.get(s.section) or {}).get(f) or "") for f, _ in s.fields}
              for s in SERVICES}

    out("Konfiguracja kluczy API dystrybutorów (zapasowe źródło dokumentów RoHS/REACH).")
    out(f"Plik: {path}\n")
    for i, svc in enumerate(SERVICES, 1):
        state = "skonfigurowany" if all(values[svc.section].values()) else "brak kluczy"
        out(f"  {i}. {svc.name:<17} {svc.rating:<15} [{state}]")
        out(f"     {svc.gives}")
    out("")
    while True:
        choice = _parse_choice(input_fn("Do których serwisów chcesz mieć dostęp? Podaj numery, np. 1,2 "
                                        "('all' = wszystkie, Enter = zakończ): "), len(SERVICES))
        if choice is not None:
            break
        out("Nie rozumiem – podaj numery z listy oddzielone przecinkiem.")
    changed = []
    for idx in choice:
        svc = SERVICES[idx]
        out(f"\n=== {svc.name} ===")
        for step in svc.signup:
            out(f"  • {step}")
        out("  (wpisywane znaki są ukryte; Enter = zostaw obecną wartość; '-' = usuń klucz)")
        for field_name, label in svc.fields:
            old = values[svc.section][field_name]
            hint = f" [obecnie: {_mask(old)}]" if old else ""
            new = secret_fn(f"  {label}{hint}: ").strip()
            if new == "-":
                values[svc.section][field_name] = ""
            elif new:
                values[svc.section][field_name] = new
        if values[svc.section] != {f: str((current.get(svc.section) or {}).get(f) or "") for f, _ in svc.fields}:
            changed.append(svc)
    if not changed:
        out("\nBez zmian.")
        return 0
    write_credentials(path, values)
    out(f"\nZapisano {path} (prawa dostępu tylko dla właściciela). Plik jest w .gitignore.")
    if test_fn is not None:
        if input_fn("Sprawdzić teraz klucze zapytaniem do API? [T/n]: ").strip().lower() in ("", "t", "y", "tak", "yes"):
            for svc in changed:
                if all(values[svc.section].values()):
                    out(f"  {svc.name}: {test_fn(svc.client)}")
    return len(changed)


def _test_client(settings, client_key: str) -> str:
    from .distributors import CLIENTS
    from .http_client import PoliteSession

    cls = CLIENTS[client_key]
    keys = cls.credentials(merged_keys(settings))
    if keys is None:
        return "brak kompletu kluczy"
    try:
        parts = cls(PoliteSession(settings), keys).lookup("LM358DR")
        return f"OK – API odpowiada (wyników dla LM358DR: {len(parts)})"
    except Exception as exc:  # noqa: BLE001 – pokazujemy każdy błąd użytkownikowi
        return f"BŁĄD – {exc}"


def main(argv: list[str] | None = None) -> int:
    from . import prepare_console

    prepare_console()
    from .config import Settings

    p = argparse.ArgumentParser(
        prog="bom_compliance.credentials",
        description="Klucze API dystrybutorów. Bez argumentów uruchamia kreator (wybór serwisów i wpisanie kluczy).")
    p.add_argument("--path", default=None, help="Plik z kluczami (domyślnie config/credentials.yaml)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("setup", help="Kreator: wybierz serwisy i wpisz klucze (domyślna komenda)")
    sub.add_parser("init", help="Utwórz pusty config/credentials.yaml ze wzoru (do ręcznej edycji)")
    s = sub.add_parser("status", help="Pokaż, które klucze są ustawione")
    s.add_argument("--check", action="store_true", help="Sprawdź klucze zapytaniem do API")
    s.add_argument("-c", "--config", help="Plik konfiguracyjny YAML")
    r = sub.add_parser("remove", help="Usuń klucze wybranego serwisu")
    r.add_argument("service", choices=[svc.section for svc in SERVICES])
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    target = Path(args.path) if args.path else DEFAULT_CREDENTIALS
    cmd = args.cmd or "setup"

    if cmd == "setup":
        settings = Settings.load(None, {"credentials_file": str(target)})
        try:
            setup_wizard(target, test_fn=lambda key: _test_client(settings, key))
        except (KeyboardInterrupt, EOFError):
            print("\nPrzerwano – nic nie zapisano.")
            return 1
        return 0
    if cmd == "init":
        if target.exists():
            print(f"{target} już istnieje – nie nadpisuję. Uruchom kreator: python -m bom_compliance.credentials")
            return 0
        write_credentials(target, {})
        print(f"Utworzono {target}. Wpisz klucze ręcznie albo uruchom kreator: python -m bom_compliance.credentials")
        return 0
    if cmd == "remove":
        raw = _read_raw(target)
        svc = next(x for x in SERVICES if x.section == args.service)
        values = {x.section: {f: str((raw.get(x.section) or {}).get(f) or "") for f, _ in x.fields} for x in SERVICES}
        values[svc.section] = {f: "" for f, _ in svc.fields}
        write_credentials(target, values)
        print(f"Usunięto klucze: {svc.name}")
        return 0
    settings = Settings.load(getattr(args, "config", None), {"credentials_file": str(target)})
    for line in status_lines(settings, check=args.check):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

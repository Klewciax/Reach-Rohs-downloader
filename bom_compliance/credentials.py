"""Dane logowania do API dystrybutorów (config/credentials.yaml).

    python -m bom_compliance.credentials                  # KREATOR: wybierz serwisy i wklej klucze (widoczne)
    python -m bom_compliance.credentials status           # pokaż wpisane klucze (--mask = zamaskowane)
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
    ("digikey", "sandbox"): "DIGIKEY_SANDBOX",
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


def clean_value(raw: str) -> tuple[str, list[str]]:
    """Usuwa typowe śmieci z wklejonego klucza i mówi, co poprawiono."""
    fixes = []
    v = raw.replace("\ufeff", "").replace("\u200b", "")
    if v != v.strip():
        fixes.append("usunięto spacje / znaki nowej linii z początku lub końca")
        v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'`":
        fixes.append("usunięto cudzysłowy wokół wartości")
        v = v[1:-1].strip()
    for prefix in ("Bearer ", "client_id=", "client_secret=", "apiKey=", "api_key="):
        if v.lower().startswith(prefix.lower()):
            fixes.append(f"usunięto przedrostek '{prefix.strip()}'")
            v = v[len(prefix):].strip()
    return v, fixes


def diagnose(field_label: str, value: str, other_values: list[str] = ()) -> list[str]:
    """Ostrzeżenia o podejrzanych wartościach (nie blokują zapisu)."""
    w = []
    if not value:
        return w
    if " " in value or "\t" in value:
        w.append(f"{field_label}: zawiera spację w środku – klucze API zwykle jej nie mają, sprawdź kopiowanie")
    if any(ord(c) > 126 or ord(c) < 32 for c in value):
        w.append(f"{field_label}: zawiera nietypowe znaki (np. polskie litery, niewidoczne znaki) – skopiuj ponownie")
    if len(value) < 8:
        w.append(f"{field_label}: bardzo krótki ({len(value)} znaków) – czy to na pewno cały klucz?")
    if "…" in value or "..." in value or value.endswith("*"):
        w.append(f"{field_label}: wygląda na skróconą/zamaskowaną wartość z portalu – skopiuj pełny klucz")
    if value in other_values:
        w.append(f"{field_label}: ma tę samą wartość co inne pole – czy nie wkleiłeś dwa razy tego samego?")
    return w


def _show(v: str, mask: bool) -> str:
    return (_mask(v) if mask else f"'{v}'") + f" ({len(v)} znaków)"


def status_lines(settings, check: bool = False, mask: bool = False, mpn: str = "LM358DR") -> list[str]:
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
        ok = all(os.environ.get(e) or keys.get(e) for e in cls.env)
        lines.append(f"  [{'OK ' if ok else ' - '}] {cls.name}")
        values = [os.environ.get(e) or keys.get(e) or "" for e in cls.env]
        for env in cls.env + cls.optional_env:
            v = os.environ.get(env) or keys.get(env)
            src = "zmienna środowiskowa" if os.environ.get(env) else "plik"
            if v:
                lines.append(f"        {env:<22} = {_show(v, mask)}  [{src}]")
                for warn in diagnose(env, v, [x for x in values if x is not v and x != ""] if env in cls.env else []):
                    lines.append(f"        UWAGA: {warn}")
            elif env in cls.env:
                lines.append(f"        {env:<22} = brak")
    if check:
        hub = DistributorHub(PoliteSession(settings), settings.distributor_sources, keys)
        for client in hub.clients:
            lines.append(f"  test {client.name}: {_test_one(client, mpn)}")
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


def setup_wizard(path: Path, input_fn=input, secret_fn=None, out=print, test_fn=None, hide: bool = False) -> int:
    """Interaktywny wybór serwisów i wpisanie kluczy. Domyślnie wpisywane wartości są WIDOCZNE
    (łatwiej sprawdzić, czy klucz wkleił się poprawnie); hide=True ukrywa je. Zwraca liczbę zmienionych serwisów."""
    if secret_fn is None:
        secret_fn = getpass.getpass if hide else input_fn
    current = _read_raw(path)
    values = {s.section: {f: str((current.get(s.section) or {}).get(f) or "") for f, _ in s.fields}
              for s in SERVICES}
    values["digikey"]["sandbox"] = str((current.get("digikey") or {}).get("sandbox") or "")

    out("Konfiguracja kluczy API dystrybutorów (zapasowe źródło dokumentów RoHS/REACH).")
    out(f"Plik: {path}\n")
    for i, svc in enumerate(SERVICES, 1):
        state = "skonfigurowany" if all(values[svc.section][f] for f, _ in svc.fields) else "brak kluczy"
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
        out("  (wklej wartość i Enter; Enter bez wartości = zostaw obecną; '-' = usuń)")
        for field_name, label in svc.fields:
            old = values[svc.section][field_name]
            if old:
                out(f"  {label} – obecnie: {_show(old, hide)}")
            raw = secret_fn(f"  {label}: ")
            if raw.strip() == "-":
                values[svc.section][field_name] = ""
                out(f"    -> usunięto")
                continue
            new, fixes = clean_value(raw)
            if not new:
                continue
            values[svc.section][field_name] = new
            for f in fixes:
                out(f"    poprawka: {f}")
            out(f"    -> zapisano {label}: {_show(new, hide)}")
        if svc.section == "digikey":
            cur = str(values["digikey"].get("sandbox") or "").lower() in ("true", "1", "tak", "t", "yes")
            ans = input_fn(f"  Czy aplikacja DigiKey jest typu Sandbox? [t/N] (obecnie: {'tak' if cur else 'nie'}): ")
            if ans.strip():
                values["digikey"]["sandbox"] = "true" if ans.strip().lower() in ("t", "tak", "y", "yes") else ""
        vals = [values[svc.section][f] for f, _ in svc.fields]
        for (field_name, label), v in zip(svc.fields, vals):
            for warn in diagnose(label, v, [x for x in vals if x is not v and x]):
                out(f"    UWAGA: {warn}")
        before = {f: str((current.get(svc.section) or {}).get(f) or "") for f in values[svc.section]}
        if values[svc.section] != before:
            changed.append(svc)
    if not changed:
        out("\nBez zmian.")
        return 0
    write_credentials(path, values)
    out(f"\nZapisano {path} (prawa dostępu tylko dla właściciela, plik jest w .gitignore).")
    out("Możesz go też otworzyć w Notatniku i sprawdzić/poprawić wartości.")
    if test_fn is not None:
        if input_fn("Sprawdzić teraz klucze zapytaniem do API? [T/n]: ").strip().lower() in ("", "t", "y", "tak", "yes"):
            for svc in changed:
                if all(values[svc.section][f] for f, _ in svc.fields):
                    out(f"  {svc.name}: {test_fn(svc.client)}")
    return len(changed)


def _test_one(client, mpn: str = "LM358DR") -> str:
    from .http_client import FetchError, LoginRequired

    try:
        parts = client.lookup(mpn)
        if not parts:
            return (f"dostęp działa, ale brak wyników dla {mpn} – uruchom z --debug, aby zobaczyć odpowiedź API")
        found = "; ".join(f"{p.mpn} ({p.manufacturer}), dokumentów: {len(p.documents)}"
                          + (f", RoHS: {p.rohs_status}" if p.rohs_status else "")
                          + (f", strona producenta: {p.manufacturer_homepage}" if p.manufacturer_homepage else "")
                          for p in parts[:3])
        return f"OK – dostęp działa, wyniki dla {mpn}: {found}"
    except LoginRequired as exc:
        hint = client.explain(exc)
        return f"BŁĄD DOSTĘPU – {exc}" + (f"\n        Co sprawdzić: {hint}" if hint else "")
    except FetchError as exc:
        if exc.status is None:
            return f"BŁĄD SIECI – {exc} (sprawdź połączenie / proxy / firewall)"
        return f"BŁĄD – {exc}"
    except Exception as exc:  # noqa: BLE001 – pokazujemy każdy błąd użytkownikowi
        return f"BŁĄD – {exc.__class__.__name__}: {exc}"


def _test_client(settings, client_key: str) -> str:
    from .distributors import CLIENTS
    from .http_client import PoliteSession

    cls = CLIENTS[client_key]
    keys = cls.credentials(merged_keys(settings))
    if keys is None:
        return "brak kompletu kluczy"
    return _test_one(cls(PoliteSession(settings), keys))


def main(argv: list[str] | None = None) -> int:
    from . import prepare_console

    prepare_console()
    from .config import Settings

    p = argparse.ArgumentParser(
        prog="bom_compliance.credentials",
        description="Klucze API dystrybutorów. Bez argumentów uruchamia kreator (wybór serwisów i wpisanie kluczy).")
    p.add_argument("--path", default=None, help="Plik z kluczami (domyślnie config/credentials.yaml)")
    sub = p.add_subparsers(dest="cmd")
    st = sub.add_parser("setup", help="Kreator: wybierz serwisy i wpisz klucze (domyślna komenda)")
    st.add_argument("--hide", action="store_true", help="Ukrywaj wpisywane klucze (domyślnie widoczne)")
    sub.add_parser("init", help="Utwórz pusty config/credentials.yaml ze wzoru (do ręcznej edycji)")
    s = sub.add_parser("status", help="Pokaż, które klucze są ustawione")
    s.add_argument("--check", action="store_true", help="Sprawdź klucze zapytaniem do API")
    s.add_argument("--mask", action="store_true", help="Zamaskuj klucze na wydruku (domyślnie widoczne)")
    s.add_argument("--mpn", default="LM358DR", help="MPN do testu (domyślnie LM358DR)")
    s.add_argument("--debug", action="store_true", help="Pokaż surowe odpowiedzi API (diagnostyka)")
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
            setup_wizard(target, test_fn=lambda key: _test_client(settings, key), hide=getattr(args, "hide", False))
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
        values["digikey"]["sandbox"] = str((raw.get("digikey") or {}).get("sandbox") or "")
        values[svc.section] = {f: "" for f in values[svc.section]}
        write_credentials(target, values)
        print(f"Usunięto klucze: {svc.name}")
        return 0
    settings = Settings.load(getattr(args, "config", None), {"credentials_file": str(target)})
    if args.debug:
        from . import distributors

        def sink(source, what, data):
            text = json.dumps(data, ensure_ascii=False, indent=1)
            print(f"--- [{source}] {what} ---\n{text[:3000]}{' …(obcięto)' if len(text) > 3000 else ''}")
        distributors.DEBUG_SINK = sink
        logging.getLogger().setLevel(logging.INFO)
    for line in status_lines(settings, check=args.check or args.debug, mask=args.mask, mpn=args.mpn):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""Dane logowania do API dystrybutorów (config/credentials.yaml).

    python -m bom_compliance.credentials init             # utwórz config/credentials.yaml ze wzoru
    python -m bom_compliance.credentials status           # które klucze są ustawione (zamaskowane)
    python -m bom_compliance.credentials status --check   # dodatkowo sprawdź klucze zapytaniem do API

Kolejność (ostatnie wygrywa): plik credentials.yaml < `api_keys:` w konfiguracji < zmienne środowiskowe.
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import stat
import sys
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
    lines.append(f"Plik z kluczami: {path} ({'jest' if path.is_file() else 'BRAK – utwórz: python -m bom_compliance.credentials init'})")
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


def main(argv: list[str] | None = None) -> int:
    from .config import Settings

    p = argparse.ArgumentParser(prog="bom_compliance.credentials", description="Klucze API dystrybutorów")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="Utwórz config/credentials.yaml ze wzoru")
    i.add_argument("--path", default=str(DEFAULT_CREDENTIALS))
    s = sub.add_parser("status", help="Pokaż, które klucze są ustawione")
    s.add_argument("--check", action="store_true", help="Sprawdź klucze zapytaniem do API")
    s.add_argument("-c", "--config", help="Plik konfiguracyjny YAML")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    if args.cmd == "init":
        target = Path(args.path)
        if target.exists():
            print(f"{target} już istnieje – nie nadpisuję. Edytuj go i wpisz klucze.")
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_CREDENTIALS, target)
        if os.name == "posix":
            target.chmod(0o600)
        print(f"Utworzono {target}. Wpisz klucze (instrukcje w komentarzach w pliku), potem:\n"
              f"  python -m bom_compliance.credentials status --check")
        return 0
    settings = Settings.load(args.config)
    for line in status_lines(settings, check=args.check):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

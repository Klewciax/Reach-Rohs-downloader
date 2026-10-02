"""bom-compliance: pobieranie deklaracji RoHS/REACH z oficjalnych stron producentów."""
from __future__ import annotations

import importlib.util
import sys

__version__ = "1.0.0"

# moduł importowany -> pakiet pip (do komunikatu)
_REQUIRED = {"yaml": "PyYAML", "requests": "requests", "bs4": "beautifulsoup4", "openpyxl": "openpyxl",
             "pypdf": "pypdf"}


def _check_dependencies() -> None:
    missing = [pkg for mod, pkg in _REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    sys.stderr.write(
        "\nBrakuje bibliotek Pythona potrzebnych do działania narzędzia: " + ", ".join(missing) + "\n\n"
        "Zainstaluj je (w folderze projektu, w tym samym terminalu):\n\n"
        "    python -m pip install -r requirements.txt\n\n"
        "Jeśli używasz środowiska wirtualnego, najpierw je aktywuj (Windows PowerShell):\n"
        "    .\\.venv\\Scripts\\Activate.ps1\n\n")
    raise SystemExit(1)


def prepare_console() -> None:
    """Konsola Windows (cp1250/cp852) nie zawsze obsługuje znaki typu ★ czy polskie litery
    przy przekierowaniu wyjścia – zamiast błędu wypisujemy znak zastępczy."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


_check_dependencies()

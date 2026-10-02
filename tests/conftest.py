import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bom_compliance.config import Settings  # noqa: E402


def make_pdf(text: str) -> bytes:
    """Minimalny, poprawny PDF z jedną stroną tekstu (do testów klasyfikacji)."""
    lines = text.split("\n")
    ops = ["BT", "/F1 10 Tf", "40 800 Td", "12 TL"]
    for ln in lines:
        esc = ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({esc}) Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@pytest.fixture
def settings(tmp_path):
    return Settings.load(None, {
        "output_dir": str(tmp_path / "out"),
        "min_delay_per_host": 0.0,
        "delay_jitter": 0.0,
        "backoff_base": 0.0,
        "max_retries": 2,
        "credentials_file": str(tmp_path / "no-credentials.yaml"),
        "discovered_manufacturers_file": str(tmp_path / "discovered.yaml"),
    })


@pytest.fixture(autouse=True)
def _isolate_user_files(tmp_path, monkeypatch):
    """Testy nie mogą czytać prawdziwych kluczy ani pisać do config/ użytkownika."""
    import bom_compliance.config as cfg

    orig = cfg.Settings.load.__func__

    def load(cls, path=None, overrides=None):
        o = {"credentials_file": str(tmp_path / "no-credentials.yaml"),
             "discovered_manufacturers_file": str(tmp_path / "discovered.yaml")}
        o.update(overrides or {})
        return orig(cls, path, o)

    monkeypatch.setattr(cfg.Settings, "load", classmethod(load))
    for v in ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET", "NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET",
              "MOUSER_API_KEY", "TME_TOKEN", "TME_APP_SECRET"):
        monkeypatch.delenv(v, raising=False)

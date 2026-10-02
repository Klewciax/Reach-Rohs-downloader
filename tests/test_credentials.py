import os

import yaml

from bom_compliance.config import Settings
from bom_compliance.credentials import EXAMPLE_CREDENTIALS, load_credentials, main, status_lines
from bom_compliance.distributors import DistributorHub
from bom_compliance.http_client import PoliteSession

ENV = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET", "NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET", "MOUSER_API_KEY",
       "TME_TOKEN", "TME_APP_SECRET")


def _clean_env(monkeypatch):
    for v in ENV:
        monkeypatch.delenv(v, raising=False)


def test_example_template_is_valid_and_empty():
    data = yaml.safe_load(EXAMPLE_CREDENTIALS.read_text(encoding="utf-8"))
    assert set(data) == {"digikey", "nexar", "mouser", "tme"}
    assert load_credentials(EXAMPLE_CREDENTIALS) == {}  # puste pola = nic nie włączone


def test_credentials_file_enables_clients(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    f = tmp_path / "credentials.yaml"
    f.write_text("digikey:\n  client_id: abc123456\n  client_secret: s3cr3t-value\nmouser:\n  api_key: ''\n")
    keys = load_credentials(f)
    assert keys == {"DIGIKEY_CLIENT_ID": "abc123456", "DIGIKEY_CLIENT_SECRET": "s3cr3t-value"}
    s = Settings.load(None, {"credentials_file": str(f)})
    from bom_compliance.credentials import merged_keys
    hub = DistributorHub(PoliteSession(s), s.distributor_sources, merged_keys(s))
    assert [c.name for c in hub.clients] == ["DigiKey"]
    assert any("Mouser" in m for m in hub.missing_keys)


def test_environment_overrides_file(tmp_path, monkeypatch):
    _clean_env(monkeypatch)
    f = tmp_path / "credentials.yaml"
    f.write_text("mouser:\n  api_key: from-file-key\n")
    monkeypatch.setenv("MOUSER_API_KEY", "from-env-key")
    s = Settings.load(None, {"credentials_file": str(f)})
    from bom_compliance.credentials import merged_keys
    hub = DistributorHub(PoliteSession(s), ["mouser"], merged_keys(s))
    assert hub.clients[0].keys["MOUSER_API_KEY"] == "from-env-key"


def test_init_and_status_mask_secrets(tmp_path, monkeypatch, capsys):
    _clean_env(monkeypatch)
    target = tmp_path / "credentials.yaml"
    assert main(["init", "--path", str(target)]) == 0
    assert target.is_file() and (os.name != "posix" or oct(target.stat().st_mode & 0o777) == "0o600")
    target.write_text("nexar:\n  client_id: ABCDEFGHIJ123\n  client_secret: VERYSECRETVALUE99\n")
    lines = "\n".join(status_lines(Settings.load(None, {"credentials_file": str(target)})))
    assert "Nexar/Octopart" in lines and "VERYSECRETVALUE99" not in lines and "VER…99" in lines
    assert "[OK ] Nexar" in lines and "[ - ] DigiKey" in lines
    assert main(["init", "--path", str(target)]) == 0  # nie nadpisuje istniejącego pliku
    assert "VERYSECRETVALUE99" in target.read_text()

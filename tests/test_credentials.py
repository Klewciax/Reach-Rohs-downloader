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
    assert main(["--path", str(target), "init"]) == 0
    assert target.is_file() and (os.name != "posix" or oct(target.stat().st_mode & 0o777) == "0o600")
    target.write_text("nexar:\n  client_id: ABCDEFGHIJ123\n  client_secret: VERYSECRETVALUE99\n")
    lines = "\n".join(status_lines(Settings.load(None, {"credentials_file": str(target)})))
    assert "Nexar/Octopart" in lines and "VERYSECRETVALUE99" not in lines and "VER…99" in lines
    assert "[OK ] Nexar" in lines and "[ - ] DigiKey" in lines
    assert main(["--path", str(target), "init"]) == 0  # nie nadpisuje istniejącego pliku
    assert "VERYSECRETVALUE99" in target.read_text()


def _wizard(path, answers, secrets, tested=None):
    from bom_compliance.credentials import setup_wizard
    a, sec, out = iter(answers), iter(secrets), []
    n = setup_wizard(path, input_fn=lambda _: next(a), secret_fn=lambda _: next(sec), out=out.append,
                     test_fn=(lambda key: tested.append(key) or "OK") if tested is not None else None)
    return n, "\n".join(out)


def test_wizard_select_services_and_enter_keys(tmp_path):
    path = tmp_path / "credentials.yaml"
    tested = []
    n, out = _wizard(path, ["1,3", "t"], ["dk-id-123456", "dk-secret-987", "mouser-key-555"], tested)
    assert n == 2 and tested == ["digikey", "mouser"]
    assert load_credentials(path) == {"DIGIKEY_CLIENT_ID": "dk-id-123456", "DIGIKEY_CLIENT_SECRET": "dk-secret-987",
                                      "MOUSER_API_KEY": "mouser-key-555"}
    assert "developer.digikey.com" in out and "dk-secret-987" not in out   # klucze nie są wypisywane
    text = path.read_text()
    assert "# DigiKey" in text and "NIE commituj" in text                    # komentarze ze wzoru zachowane
    assert os.name != "posix" or oct(path.stat().st_mode & 0o777) == "0o600"

    # Ponowne uruchomienie: Enter zostawia wartość, '-' usuwa, inny serwis bez zmian
    n, out = _wizard(path, ["1", "n"], ["", "-"], [])
    assert "obecnie" not in out or "dk-id-123456" not in out
    keys = load_credentials(path)
    assert keys["DIGIKEY_CLIENT_ID"] == "dk-id-123456" and "DIGIKEY_CLIENT_SECRET" not in keys
    assert keys["MOUSER_API_KEY"] == "mouser-key-555"


def test_wizard_invalid_choice_then_quit(tmp_path):
    path = tmp_path / "credentials.yaml"
    n, out = _wizard(path, ["9", ""], [])
    assert n == 0 and "Nie rozumiem" in out and not path.exists()


def test_wizard_all_and_special_characters(tmp_path):
    path = tmp_path / "credentials.yaml"
    secrets = ["id:with#hash", 'sec"quote\\x', "nx-id", "nx-sec", "mk", "tok en", "s3cr3t"]
    n, _ = _wizard(path, ["all"], secrets)
    keys = load_credentials(path)
    assert n == 4 and keys["DIGIKEY_CLIENT_ID"] == "id:with#hash" and keys["DIGIKEY_CLIENT_SECRET"] == 'sec"quote\\x'
    assert keys["TME_TOKEN"] == "tok en"


def test_remove_command(tmp_path):
    path = tmp_path / "credentials.yaml"
    _wizard(path, ["3"], ["mouser-key-555"])
    assert main(["--path", str(path), "remove", "mouser"]) == 0
    assert load_credentials(path) == {}

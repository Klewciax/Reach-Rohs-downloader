import pytest

from bom_compliance.config import DEFAULT_MANUFACTURERS
from bom_compliance.manufacturers import ManufacturerRegistry, normalize_name, strip_legal
from bom_compliance.models import ManufacturerInfo


@pytest.fixture(scope="module")
def registry():
    return ManufacturerRegistry.from_yaml(DEFAULT_MANUFACTURERS)


@pytest.mark.parametrize("raw", ["TI", "Texas Instruments", "Texas Instruments Inc.", "TEXAS INSTRUMENTS INCORPORATED",
                                 "texas instruments, inc", "Burr-Brown"])
def test_ti_variants(registry, raw):
    m, _ = registry.resolve(raw)
    assert m is not None and m.key == "texas_instruments"


@pytest.mark.parametrize("raw,key", [
    ("ON Semiconductor", "onsemi"), ("onsemi", "onsemi"), ("Fairchild Semiconductor", "onsemi"),
    ("STMicroelectronics N.V.", "stmicroelectronics"), ("Linear Technology Corp.", "analog_devices"),
    ("Maxim Integrated", "analog_devices"), ("Würth Elektronik", "wurth"), ("Wuerth Elektronik GmbH", "wurth"),
    ("Vishay Dale", "vishay"), ("Murata Manufacturing Co., Ltd.", "murata"), ("NXP USA Inc.", "nxp"),
])
def test_aliases(registry, raw, key):
    m, _ = registry.resolve(raw)
    assert m is not None and m.key == key


def test_fuzzy_and_unknown(registry):
    m, method = registry.resolve("Texas Instrumments")
    assert m.key == "texas_instruments" and method == "fuzzy"
    assert registry.resolve("Acme Connectors Ltd")[0] is None
    assert registry.resolve("")[0] is None


def test_normalization():
    assert normalize_name("Würth Elektronik eiSos GmbH & Co. KG") == "WURTH ELEKTRONIK EISOS GMBH AND CO KG"
    assert strip_legal(normalize_name("Texas Instruments, Inc.")) == "TEXAS INSTRUMENTS"


def test_conflicting_alias_rejected():
    a = ManufacturerInfo("a", "Alpha", ["a.com"], aliases=["XYZ"])
    b = ManufacturerInfo("b", "Beta", ["b.com"], aliases=["XYZ Inc"])
    with pytest.raises(ValueError):
        ManufacturerRegistry([a, b])

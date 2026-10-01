import pytest
from openpyxl import Workbook

from bom_compliance.bom import group_items, read_bom
from bom_compliance.config import DEFAULT_MANUFACTURERS
from bom_compliance.manufacturers import ManufacturerRegistry


@pytest.fixture(scope="module")
def registry():
    return ManufacturerRegistry.from_yaml(DEFAULT_MANUFACTURERS)


def test_csv_autodetect_dedup_and_invalid(tmp_path, registry):
    p = tmp_path / "bom.csv"
    p.write_text(
        "Designator;Manufacturer;Manufacturer Part Number;Qty\n"
        "Q1,Q2;ON Semiconductor;NDS331N;2\n"
        "Q3;onsemi;NDS331N;1\n"
        "U1;Texas Instruments;LM358DR;1\n"
        "U2;STMicroelectronics;LM358DR;1\n"   # ten sam MPN, inny producent -> osobna pozycja
        "R1;;RC0603FR-0710KL;1\n"
        "TP1;Generic;N/A;1\n"
        ";;;\n",
        encoding="utf-8",
    )
    rows, invalid, meta = read_bom(p)
    assert meta["columns"]["mpn"] == "Manufacturer Part Number"
    assert len(rows) == 4 and len(invalid) == 2 and meta["empty_rows"] == 1
    items = group_items(rows, registry)
    assert len(items) == 3
    nds = next(i for i in items if i.mpn == "NDS331N")
    assert nds.refdes == ["Q1", "Q2", "Q3"] and nds.manufacturer.key == "onsemi"
    assert {i.manufacturer.key for i in items if i.mpn == "LM358DR"} == {"texas_instruments", "stmicroelectronics"}


def test_explicit_mapping_and_errors(tmp_path):
    p = tmp_path / "bom.csv"
    p.write_text("Ref,Maker Name,Supplier PN,Order Code\nU1,TI,LM358DR,DK-123\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_bom(p)  # brak rozpoznawalnych kolumn
    rows, _, _ = read_bom(p, {"mpn": "Supplier PN", "manufacturer": "Maker Name"})
    assert rows[0].mpn == "LM358DR" and rows[0].manufacturer == "TI"
    with pytest.raises(ValueError):
        read_bom(p, {"mpn": "Nope", "manufacturer": "Maker Name"})


def test_xlsx_header_not_on_first_row(tmp_path):
    wb = Workbook()
    ws = wb.active
    ws.append(["Project X BoM rev B"])
    ws.append([])
    ws.append(["Ref Des", "Mfr", "Mfr Part Number"])
    ws.append(["U1", "Nexperia", "BAS16,215"])
    p = tmp_path / "bom.xlsx"
    wb.save(p)
    rows, invalid, meta = read_bom(p)
    assert meta["header_row"] == 3 and rows[0].mpn == "BAS16,215" and rows[0].refdes == ["U1"]

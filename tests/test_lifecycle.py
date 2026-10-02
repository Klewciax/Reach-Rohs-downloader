import re

import responses

from bom_compliance.cli import main
from bom_compliance.lifecycle import classify_status, parse_lifecycle, parse_longevity
from bom_compliance.models import LifecycleStatus
from conftest import make_pdf


def test_classify_status_labels():
    assert classify_status("Not Recommended for new designs") == LifecycleStatus.NRND
    assert classify_status("NRND") == LifecycleStatus.NRND
    assert classify_status("LIFEBUY") == LifecycleStatus.LAST_TIME_BUY
    assert classify_status("Last Time Buy") == LifecycleStatus.LAST_TIME_BUY
    assert classify_status("Obsolete") == LifecycleStatus.OBSOLETE
    assert classify_status("discontinued") == LifecycleStatus.OBSOLETE
    assert classify_status("In Production") == LifecycleStatus.ACTIVE
    assert classify_status("active and preferred") == LifecycleStatus.ACTIVE
    assert classify_status("Preview") == LifecycleStatus.PREVIEW
    assert classify_status("Mature") == LifecycleStatus.MATURE


def test_status_bound_to_exact_mpn_not_neighbour_variant():
    text = "Orderable part LM358DRG3 Obsolete SOIC LM358DR Active SOIC 2500 LM358DRG4 Obsolete"
    st, label, scope, _ = parse_lifecycle(text, "LM358DR")
    assert (st, scope) == (LifecycleStatus.ACTIVE, "part")


def test_page_level_status_ignores_navigation_links():
    text = "Menu Find/Replace Obsolete/EOL Products | ATmega328P | Status: Not Recommended for new designs"
    st, _, scope, _ = parse_lifecycle(text, "ATMEGA328P-AU")
    assert (st, scope) == (LifecycleStatus.NRND, "page")


def test_unknown_when_no_label():
    assert parse_lifecycle("Datasheet, pricing, packaging", "ABC123")[0] == LifecycleStatus.UNKNOWN


def test_longevity_parsing():
    info = parse_longevity("STM32F103 family 10 years starting 2023", "STM32F103C8T6")
    assert info.scope == "family" and info.years == 10 and info.end_year == 2033 and info.end_basis == "start+years"
    info = parse_longevity("S912XEG128J2CAAR longevity until 2033", "S912XEG128J2CAAR")
    assert info.scope == "part" and info.end_year == 2033 and info.end_basis == "explicit"
    assert parse_longevity("No such parts here", "XYZ12345") is None


@responses.activate
def test_full_run_with_lifecycle_and_longevity(tmp_path):
    for host in ("www.ti.com", "www.microchip.com", "ww1.microchip.com", "www.nxp.com"):
        responses.get(f"https://{host}/robots.txt", status=404)
    # TI: strona sklepu dla MPN z tabelą wariantów
    responses.get("https://www.ti.com/store/ti/en/p/product/?p=LM358DR",
                  body="<html><body><a href='/x'>Find Obsolete products</a><table>"
                       "<tr><td>LM358DRG3</td><td>Obsolete</td></tr><tr><td>LM358DR</td><td>Active</td></tr>"
                       "</table></body></html>", content_type="text/html")
    # Microchip: strona produktu bazowego; status strony (rodziny)
    responses.get("https://www.microchip.com/en-us/product/ATMEGA328P",
                  body="<html><body><h1>ATmega328P</h1><div>Status: Not Recommended for new designs</div>"
                       "</body></html>", content_type="text/html")
    responses.get("https://ww1.microchip.com/downloads/aemDocuments/documents/quality---reliability/Product-Longevity.pdf",
                  body=make_pdf("Our Practice on Product Longevity\nClient-driven obsolescence"),
                  content_type="application/pdf")
    # NXP: tabela programu longevity
    responses.get("https://www.nxp.com/products/nxp-product-information/nxp-product-programs/product-longevity:PRDCT_LONGEVITY_HM",
                  body="<html><body><table><tr><th>Part</th><th>Launch</th><th>Longevity (years)</th><th>End date</th></tr>"
                       "<tr><td>S912XEG128J2CAAR</td><td>2012</td><td>15</td><td>2033</td></tr></table></body></html>",
                  content_type="text/html")
    responses.get(re.compile(r"https://.*"), status=404)

    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("min_delay_per_host: 0\ndelay_jitter: 0\nbackoff_base: 0\nmax_retries: 0\n"
                   "download_general_statements: false\ngeneric_max_pages: 0\n")
    bom = tmp_path / "bom.csv"
    bom.write_text("Ref,Manufacturer,MPN\nU1,TI,LM358DR\nU2,Microchip,ATMEGA328P-AU\nU3,NXP,S912XEG128J2CAAR\n",
                   encoding="utf-8")
    out = tmp_path / "out"
    assert main([str(bom), "-o", str(out), "-c", str(cfg), "--lifecycle", "--longevity", "--no-contacts"]) == 0

    import csv
    rows = {r["MPN"]: r for r in csv.DictReader(open(out / "report_lifecycle.csv", encoding="utf-8-sig"), delimiter=";")}
    assert rows["LM358DR"]["Status cyklu życia"].startswith("ACTIVE")
    assert rows["LM358DR"]["Zakres statusu"] == "dla MPN"
    assert rows["LM358DR"]["Kopia strony"].endswith(".html")
    assert rows["ATMEGA328P-AU"]["Status cyklu życia"].startswith("NRND")
    assert rows["ATMEGA328P-AU"]["Zakres statusu"] == "strona produktu / rodziny"
    assert rows["ATMEGA328P-AU"]["Longevity: znaleziono"] == "NIE"
    assert "Product-Longevity" in rows["ATMEGA328P-AU"]["Plik longevity"]
    assert rows["S912XEG128J2CAAR"]["Longevity: znaleziono"] == "TAK"
    assert rows["S912XEG128J2CAAR"]["Do kiedy (rok)"] == "2033"
    assert rows["S912XEG128J2CAAR"]["Okres (lata)"] == "15"
    # Pozycje bez daty longevity trafiają do listy mailowej z prośbą o longevity
    tpl = (out / "email_templates" / "Microchip_Technology.txt").read_text()
    assert "longevity" in tpl.lower() and "ATMEGA328P-AU" in tpl
    from openpyxl import load_workbook
    wb = load_workbook(out / "report.xlsx")
    ws = wb["Długość produkcji"]
    header = [c.value for c in ws[1]]
    row = next(r for r in ws.iter_rows(min_row=2) if r[header.index("MPN")].value == "S912XEG128J2CAAR")
    status = row[header.index("Status")]
    assert "2033" in status.value and status.hyperlink is not None
    assert (out / status.hyperlink.target).is_file()  # kopia listy longevity w folderze Dlugosc_produkcji
    assert status.hyperlink.target.startswith("documents/Dlugosc_produkcji/")
    ws = wb["Status cyklu życia"]
    header = [c.value for c in ws[1]]
    row = next(r for r in ws.iter_rows(min_row=2) if r[header.index("MPN")].value == "LM358DR")
    assert row[header.index("Status")].value.startswith("ACTIVE")
    assert row[header.index("Status")].hyperlink.target.startswith("documents/Status_cyklu_zycia/")

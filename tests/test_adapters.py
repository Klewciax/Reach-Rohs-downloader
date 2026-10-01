import responses

from bom_compliance.adapters import AdapterContext, get_adapter
from bom_compliance.config import DEFAULT_MANUFACTURERS
from bom_compliance.downloader import Downloader
from bom_compliance.http_client import PoliteSession
from bom_compliance.manufacturers import ManufacturerRegistry
from bom_compliance.models import BomItem, Scope, Status
from bom_compliance.pipeline import Pipeline

REG = ManufacturerRegistry.from_yaml(DEFAULT_MANUFACTURERS)


def ctx_for(key, settings):
    m = REG.manufacturers[key]
    return AdapterContext(PoliteSession(settings, sleep=lambda _: None), settings, m)


def item(mpn, key):
    m = REG.manufacturers[key]
    return BomItem(mpn=mpn, manufacturer_raw=m.name, manufacturer=m, match_method="exact")


@responses.activate
def test_st_estore_material_declaration_link(settings):
    responses.get("https://estore.st.com/robots.txt", status=404)
    responses.get("https://estore.st.com/en/stm32f103c8t6-cpn.html",
                  body='<html><a href="https://www.st.com/resource/en/material_declaration/201a_413xxx6.pdf">'
                       'Material Declaration</a><a href="/en/other.pdf">Datasheet</a></html>',
                  content_type="text/html")
    settings.download_general_statements = False
    res = get_adapter("st").search("STM32F103C8T6", ctx_for("stmicroelectronics", settings))
    md = [c for c in res.candidates if "material_declaration" in c.url]
    assert len(md) == 1 and md[0].scope == Scope.PART


def test_microchip_pattern_variants(settings):
    settings.download_general_statements = False
    settings.generic_max_pages = 0
    res = get_adapter("microchip").search("PIC16F877A-I/P", ctx_for("microchip", settings))
    urls = [c.url for c in res.candidates]
    assert urls[0].endswith("EU-RoHS-CoC_PIC16F877A-I%2FP.pdf")
    assert urls[1].endswith("EU-RoHS-CoC_PIC16F877A-I.pdf")


@responses.activate
def test_html_error_page_instead_of_pdf_is_not_found(settings, tmp_path):
    responses.get("https://www.onsemi.com/robots.txt", status=404)
    responses.get("https://www.onsemi.com/PowerSolutions/MaterialComposition.do",
                  body="<html><body>Part not found</body></html>", content_type="text/html")
    settings.download_general_statements = False
    settings.generic_max_pages = 0
    res = Pipeline(settings, tmp_path, PoliteSession(settings, sleep=lambda _: None)).process_item(
        item("NOPE123", "onsemi"))
    assert res.rohs_status == Status.NOT_FOUND and not res.docs
    assert any("stronę HTML" in n for n in res.notes)


@responses.activate
def test_login_page_detected(settings, tmp_path):
    responses.get("https://www.onsemi.com/robots.txt", status=404)
    responses.get("https://www.onsemi.com/PowerSolutions/MaterialComposition.do",
                  body='<html><form><input type="password" name="pw"></form></html>', content_type="text/html")
    settings.download_general_statements = False
    settings.generic_max_pages = 0
    res = Pipeline(settings, tmp_path, PoliteSession(settings, sleep=lambda _: None)).process_item(
        item("NDS331N", "onsemi"))
    assert res.rohs_status == Status.LOGIN_REQUIRED


def test_downloader_rejects_foreign_domain(settings, tmp_path):
    from bom_compliance.models import Candidate, DocType
    import pytest
    from bom_compliance.http_client import DomainNotAllowed

    d = Downloader(PoliteSession(settings, sleep=lambda _: None), settings, tmp_path)
    with pytest.raises(DomainNotAllowed):
        d.download(Candidate("https://www.mouser.com/x.pdf", {DocType.ROHS}, Scope.PART), item("X1", "onsemi"))

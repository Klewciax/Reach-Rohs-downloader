"""Dystrybutorzy jako zapasowe źródło i automatyczne wykrywanie producentów (HTTP zamockowane)."""
import json
import re

import pytest
import responses

from bom_compliance.config import DEFAULT_MANUFACTURERS, Settings
from bom_compliance.discovery import guess_domains, name_matches_site
from bom_compliance.distributors import DistributorHub, tme_signature
from bom_compliance.http_client import PoliteSession
from bom_compliance.manufacturers import ManufacturerRegistry
from bom_compliance.models import BomItem, Scope, Status
from bom_compliance.pipeline import Pipeline
from conftest import make_pdf

DK_TOKEN = "https://api.digikey.com/v1/oauth2/token"
DK = "https://api.digikey.com/products/v4/search"


def _settings(tmp_path, **kw):
    base = {"output_dir": str(tmp_path), "min_delay_per_host": 0, "delay_jitter": 0, "max_retries": 0,
            "download_general_statements": False, "generic_max_pages": 3, "check_lifecycle": False,
            "check_longevity": False, "inspect_mpn": False,
            "discovered_manufacturers_file": str(tmp_path / "discovered.yaml")}
    base.update(kw)
    return Settings.load(None, base)


def _pipeline(tmp_path, **kw):
    s = _settings(tmp_path, **kw)
    reg = ManufacturerRegistry.from_yaml(DEFAULT_MANUFACTURERS)
    return Pipeline(s, tmp_path, PoliteSession(s, sleep=lambda _: None), registry=reg), reg


def _item(reg, mpn, mfr):
    m, method = reg.resolve(mfr)
    return BomItem(mpn=mpn, manufacturer_raw=mfr, manufacturer=m, match_method=method, mpn_bom=mpn)


def _digikey(mpn, manufacturer, media):
    responses.post(DK_TOKEN, json={"access_token": "tok", "expires_in": 600})
    responses.get(f"{DK}/{mpn}/productdetails", json={"Product": {
        "ManufacturerProductNumber": mpn, "Manufacturer": {"Name": manufacturer},
        "DatasheetUrl": "https://www.acme-components.com/ds.pdf", "ProductUrl": "https://www.digikey.com/x",
        "Classifications": {"RohsStatus": "ROHS3 Compliant", "ReachStatus": "REACH Unaffected"},
        "ProductStatus": {"Id": 0, "Status": "Active"}}})
    responses.get(f"{DK}/{mpn}/media", json={"MediaLinks": media})


@responses.activate
def test_digikey_fallback_downloads_manufacturer_document(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    responses.get("https://www.onsemi.com/robots.txt", status=404)
    responses.get("https://mm.digikey.com/robots.txt", status=404)
    _digikey("NDS331N", "onsemi", [
        {"MediaType": "Datasheets", "Title": "NDS331N datasheet", "Url": "https://mm.digikey.com/ds.pdf"},
        {"MediaType": "Environmental Information", "Title": "RoHS/REACH Cert",
         "Url": "https://mm.digikey.com/Volume0/onsemi-rohs-reach.pdf"}])
    responses.get("https://mm.digikey.com/Volume0/onsemi-rohs-reach.pdf",
                  body=make_pdf("onsemi\nCERTIFICATE OF COMPLIANCE RoHS and REACH\nNDS331N\nNo SVHC"),
                  content_type="application/pdf")
    responses.get(re.compile(r"https://.*"), status=404)
    p, reg = _pipeline(tmp_path)
    res = p.process_item(_item(reg, "NDS331N", "ON Semiconductor"))
    assert res.rohs_status == Status.DOWNLOADED and res.reach_status == Status.DOWNLOADED
    doc = res.docs[0]
    assert doc.source == "DigiKey" and doc.issuer and "__z_DigiKey" in doc.path
    assert not any("ds.pdf" in c.request.url for c in responses.calls)  # sama karta katalogowa pominięta
    assert res.distributor_parts[0].rohs_status == "ROHS3 Compliant"


@responses.activate
def test_distributor_document_without_manufacturer_name_is_not_a_declaration(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    responses.get("https://mm.digikey.com/robots.txt", status=404)
    _digikey("NDS331N", "onsemi", [{"MediaType": "Environmental Information", "Title": "RoHS statement",
                                    "Url": "https://mm.digikey.com/dk-rohs.pdf"}])
    responses.get("https://mm.digikey.com/dk-rohs.pdf",
                  body=make_pdf("Digi-Key Electronics RoHS statement\nNDS331N is RoHS compliant 2011/65/EU"),
                  content_type="application/pdf")
    responses.get(re.compile(r"https://.*"), status=404)
    p, reg = _pipeline(tmp_path)
    res = p.process_item(_item(reg, "NDS331N", "onsemi"))
    assert res.docs and res.docs[0].scope == Scope.GENERAL
    assert res.rohs_status not in (Status.DOWNLOADED, Status.DOWNLOADED_FAMILY)


@responses.activate
def test_same_mpn_from_other_manufacturer_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("DIGIKEY_CLIENT_ID", "id")
    monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "secret")
    _digikey("LM358DR", "STMicroelectronics", [{"MediaType": "Environmental Information", "Title": "RoHS Cert",
                                                "Url": "https://mm.digikey.com/st.pdf"}])
    responses.get(re.compile(r"https://.*"), status=404)
    p, reg = _pipeline(tmp_path)
    res = p.process_item(_item(reg, "LM358DR", "Texas Instruments"))
    assert not res.distributor_parts and not res.docs
    assert not any(c.request.url.endswith("st.pdf") for c in responses.calls)


@responses.activate
def test_unknown_manufacturer_domain_discovered_and_crawled(tmp_path):
    responses.get("https://www.acmeconnectors.com/robots.txt", status=404)
    responses.get("https://www.acmeconnectors.com/",
                  body="<html><head><title>ACME Connectors | Industrial connectors</title></head><body>"
                       "<a href='/quality/environment'>Environmental compliance</a></body></html>",
                  content_type="text/html")
    responses.get("https://www.acmeconnectors.com/quality/environment",
                  body="<html><a href='/docs/AC-1234_RoHS_REACH.pdf'>AC-1234 RoHS REACH declaration</a></html>",
                  content_type="text/html")
    responses.get("https://www.acmeconnectors.com/docs/AC-1234_RoHS_REACH.pdf",
                  body=make_pdf("ACME Connectors Ltd\nDeclaration of conformity RoHS 2011/65/EU and REACH SVHC\n"
                                "Part number: AC-1234"), content_type="application/pdf")
    responses.get(re.compile(r"https?://.*"), status=404)
    p, reg = _pipeline(tmp_path)
    item = _item(reg, "AC-1234", "Acme Connectors Ltd")
    assert item.manufacturer is None
    res = p.process_item(item)
    assert item.manufacturer.domains == ["acmeconnectors.com"] and item.match_method == "auto"
    assert res.rohs_status == Status.DOWNLOADED and res.reach_status == Status.DOWNLOADED
    assert any("wykryta automatycznie" in n for n in res.notes)
    p.discovery.save()
    saved = (tmp_path / "discovered.yaml").read_text()
    assert "acmeconnectors.com" in saved and "auto_discovered" in saved
    # Kolejne uruchomienie: producent jest już w rejestrze
    reg2 = ManufacturerRegistry.from_yaml(DEFAULT_MANUFACTURERS)
    reg2.merge_yaml(tmp_path / "discovered.yaml")
    assert reg2.resolve("ACME Connectors")[0].domains == ["acmeconnectors.com"]


@responses.activate
def test_discovery_rejects_site_of_other_company(tmp_path):
    responses.get(re.compile(r"https?://[^/]+/robots.txt"), status=404)
    responses.get("https://www.acmeconnectors.com/", body="<html><title>Domain for sale</title></html>",
                  content_type="text/html")
    responses.get(re.compile(r"https?://.*"), status=404)
    p, reg = _pipeline(tmp_path)
    item = _item(reg, "AC-1234", "Acme Connectors Ltd")
    res = p.process_item(item)
    assert item.manufacturer is None and res.rohs_status == Status.UNKNOWN_MANUFACTURER


@responses.activate
def test_nexar_homepage_used_for_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXAR_CLIENT_ID", "id")
    monkeypatch.setenv("NEXAR_CLIENT_SECRET", "secret")
    responses.post("https://identity.nexar.com/connect/token", json={"access_token": "t", "expires_in": 3600})
    responses.post("https://api.nexar.com/graphql", json={"data": {"supSearchMpn": {"results": [{"part": {
        "mpn": "ZX-77", "manufacturer": {"name": "Zeta Semi", "homepageUrl": "https://www.zpd-power.com/"},
        "bestDatasheet": None, "documentCollections": []}}]}}})
    responses.get("https://www.zpd-power.com/robots.txt", status=404)
    responses.get("https://www.zpd-power.com/", body="<title>Zeta Semi - power devices</title>",
                  content_type="text/html")
    responses.get(re.compile(r"https?://.*"), status=404)
    p, reg = _pipeline(tmp_path)
    item = _item(reg, "ZX-77", "Zeta Semi Inc.")
    p.process_item(item)
    # Domena niemożliwa do odgadnięcia z nazwy – znaleziona dzięki homepageUrl z Nexar/Octopart
    assert item.manufacturer.domains == ["zpd-power.com"] and item.match_method == "auto"
    body = json.loads(next(c.request.body for c in responses.calls if "graphql" in c.request.url))
    assert body["variables"] == {"q": "ZX-77"} and "homepageUrl" in body["query"]


@responses.activate
def test_mouser_parsing_and_missing_keys(tmp_path, monkeypatch):
    for v in ("NEXAR_CLIENT_ID", "DIGIKEY_CLIENT_ID", "TME_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("MOUSER_API_KEY", "k")
    responses.post(re.compile(r"https://api\.mouser\.com/api/v1/search/partnumber.*"), json={
        "Errors": [], "SearchResults": {"NumberOfResult": 1, "Parts": [{
            "ManufacturerPartNumber": "LM358DR", "Manufacturer": "Texas Instruments", "ROHSStatus": "RoHS Compliant",
            "LifecycleStatus": "New Product", "DataSheetUrl": "https://www.ti.com/lit/ds/symlink/lm358.pdf"}]}})
    s = _settings(tmp_path)
    hub = DistributorHub(PoliteSession(s, sleep=lambda _: None), ["nexar", "digikey", "mouser", "tme"])
    assert [c.name for c in hub.clients] == ["Mouser"] and len(hub.missing_keys) == 3
    parts = hub.lookup("LM358DR")
    assert parts[0].rohs_status == "RoHS Compliant" and parts[0].manufacturer == "Texas Instruments"
    sent = json.loads(responses.calls[0].request.body)
    assert sent == {"SearchByPartRequest": {"mouserPartNumber": "LM358DR", "partSearchOptions": "Exact"}}


@responses.activate
def test_tme_signed_requests(tmp_path, monkeypatch):
    monkeypatch.setenv("TME_TOKEN", "tok")
    monkeypatch.setenv("TME_APP_SECRET", "sec")
    responses.post("https://api.tme.eu/Products/Search.json", json={"Status": "OK", "Data": {"ProductList": [
        {"Symbol": "LM358DR-TI", "OriginalSymbol": "LM358DR", "Producer": "TEXAS INSTRUMENTS",
         "ProductInformationPage": "//www.tme.eu/en/details/lm358dr-ti/"}]}})
    responses.post("https://api.tme.eu/Products/GetProductsFiles.json", json={"Status": "OK", "Data": {
        "ProductList": [{"Symbol": "LM358DR-TI", "Files": {"DocumentList": [
            {"DocumentUrl": "//www.tme.eu/Document/abc/rohs-declaration.pdf", "DocumentType": "DCL",
             "DocumentName": "RoHS declaration"}]}}]}})
    s = _settings(tmp_path)
    hub = DistributorHub(PoliteSession(s, sleep=lambda _: None), ["tme"])
    parts = hub.lookup("LM358DR")
    assert parts[0].documents[0].url == "https://www.tme.eu/Document/abc/rohs-declaration.pdf"
    from urllib.parse import parse_qs
    sent = parse_qs(responses.calls[0].request.body)
    params = {k: v[0] for k, v in sent.items() if k != "ApiSignature"}
    assert sent["ApiSignature"][0] == tme_signature("POST", "https://api.tme.eu/Products/Search.json", params, "sec")


def test_guess_domains_and_name_check():
    assert "acmeconnectors.com" in guess_domains("Acme Connectors Ltd")
    assert "acme.com" in guess_domains("Acme Connectors Ltd")
    assert name_matches_site("Acme Connectors Ltd", "<title>ACME Connectors | Home</title>")[0]
    assert not name_matches_site("Acme Connectors Ltd", "<title>Domain for sale</title>")[0]

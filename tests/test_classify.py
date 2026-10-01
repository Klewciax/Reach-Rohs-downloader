from bom_compliance.classify import scope_from_text, sniff_kind, types_from_link, types_from_text
from bom_compliance.models import DocType, Scope


def test_types_from_text():
    assert types_from_text("Certificate of Compliance RoHS and REACH") == {DocType.ROHS, DocType.REACH}
    assert types_from_text("complies with 2011/65/EU; no SVHC above 0.1%") == {DocType.ROHS, DocType.REACH}
    assert DocType.REACH not in types_from_text("please reach out to our sales team")
    assert DocType.MCD in types_from_text("IPC-1752A material declaration")


def test_types_from_link():
    assert types_from_link("https://x.com/docs/EU-RoHS-CoC_ABC.pdf") == {DocType.ROHS}
    assert types_from_link("https://x.com/EU-REACH-Statement_X.pdf") == {DocType.REACH}
    assert not types_from_link("https://x.com/research/outreach.pdf")


def test_scope_from_text():
    assert scope_from_text("LM358DR", "Part: LM358-DR ...")[0] == Scope.PART  # separatory ignorowane
    assert scope_from_text("CRCW060310K0FKEA", "CRCW0603 e3 series")[0] == Scope.FAMILY
    assert scope_from_text("LM358DR", "All TI products comply with RoHS")[0] == Scope.GENERAL


def test_sniff_kind():
    assert sniff_kind(b"%PDF-1.7 ...") == "pdf"
    assert sniff_kind(b"<!DOCTYPE html><html>") == "html"
    assert sniff_kind(b"<?xml version='1.0'?><MainDeclaration/>") == "xml"

"""Adaptery dla popularnych producentów półprzewodników.

Każdy adapter korzysta z udokumentowanego / stałego wzorca URL producenta tam,
gdzie taki istnieje (zamiast przeszukiwać HTML). Wzorce pochodzą z oficjalnych
domen producentów (stan: 2026-10). Gdy producent zmieni serwis, adapter zwróci
NOT_FOUND / ERROR, a raport wskaże stronę do ręcznej weryfikacji – narzędzie
nigdy nie "zgaduje" adresów spoza domeny producenta.
"""
from __future__ import annotations

import re

from ..models import Candidate, DocType, Scope, SearchResult
from .base import AdapterContext, BaseAdapter


class TexasInstrumentsAdapter(BaseAdapter):
    """TI: dane materiałowe/certyfikaty per MPN są w narzędziu Material Content Search,
    które wymaga zalogowania na konto myTI (ti.com/materialcontent/home).
    Narzędzie nie obchodzi logowania – oznacza pozycję jako LOGIN_REQUIRED i pobiera
    publiczne ogólne oświadczenia TI (RoHS/REACH) jako materiał pomocniczy.
    """

    key = "ti"
    generic_fallback = False
    MATERIAL_CONTENT_URL = "https://www.ti.com/materialcontent/home"

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        result.login_required.append(self.MATERIAL_CONTENT_URL)
        result.notes.append(
            "TI: certyfikat RoHS / raport materiałowy per MPN dostępny w Material Content Search po "
            "zalogowaniu (myTI) – pobierz ręcznie lub poproś TI o dokumenty"
        )


class AnalogDevicesAdapter(BaseAdapter):
    """ADI (w tym Linear Technology, Maxim): strona deklaracji materiałowych z parametrem part."""

    key = "adi"
    PAGE = "https://www.analog.com/en/about-adi/quality-reliability/material-declarations.html?part={mpn}"
    SEARCH_TOOL = "https://quality.analog.com/searchresults.aspx"

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        url = self.PAGE.format(mpn=self.q(mpn))
        page = self.fetch_html(url, ctx, result)
        found = 0
        if page:
            soup, final = page
            found = self.collect_document_links(
                soup, final, mpn, ctx, result, default_types={DocType.MCD, DocType.ROHS, DocType.REACH})
        if not found:
            result.manual_urls.extend([url, self.SEARCH_TOOL])
            result.notes.append(
                "ADI: brak linków do deklaracji w statycznym HTML (strona może ładować dane przez JavaScript) – "
                "sprawdź ręcznie w Material Declaration Search")


class STMicroelectronicsAdapter(BaseAdapter):
    """ST: deklaracje materiałowe (IPC-1752, z sekcją RoHS i REACH SVHC) są linkowane ze strony
    commercial part number w eStore: estore.st.com/en/{cpn}-cpn.html; pliki w /resource/en/material_declaration/.
    """

    key = "st"
    ESTORE = "https://estore.st.com/en/{cpn}-cpn.html"

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        url = self.ESTORE.format(cpn=self.q(mpn.lower()))
        page = self.fetch_html(url, ctx, result)
        if not page:
            result.manual_urls.append(url)
            return
        soup, final = page
        n = self.collect_document_links(
            soup, final, mpn, ctx, result, url_must_contain="/material_declaration/",
            default_types={DocType.MCD, DocType.ROHS, DocType.REACH})
        # Deklaracja jest powiązana z CPN przez stronę eStore – traktujemy ją jako dokument części;
        # treść i tak jest weryfikowana po pobraniu.
        for c in result.candidates:
            if "/material_declaration/" in c.url and c.scope == Scope.GENERAL:
                c.scope = Scope.PART
                c.note = f"powiązane z CPN na stronie {final}"
        if not n:
            result.notes.append("ST: strona eStore nie zawiera linku do deklaracji materiałowej")
            result.manual_urls.append(final)


class MicrochipAdapter(BaseAdapter):
    """Microchip: certyfikaty RoHS per część publikowane wg wzorca EU-RoHS-CoC_{MPN}.pdf.
    Pełne dane (EU RoHS, REACH, China RoHS, MCD) – narzędzie PMC Search na stronie Microchip.
    """

    key = "microchip"
    ROHS_COC = ("https://ww1.microchip.com/downloads/aemDocuments/documents/corporate-responsibilty/"
                "environmental/material-compliance-documents/EU-RoHS-CoC_{mpn}.pdf")
    PMC_PAGE = "https://www.microchip.com/en-us/about/corporate-responsibility/our-products"

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        variants = [mpn]
        # Microchip: część po "/" to zwykle obudowa/opakowanie (np. PIC16F877A-I/P), część po "-" to temp/opcje.
        base = re.split(r"/", mpn)[0]
        if base != mpn:
            variants.append(base)
        for i, v in enumerate(variants):
            result.add(Candidate(
                url=self.ROHS_COC.format(mpn=self.q(v)), doc_types={DocType.ROHS},
                scope=Scope.PART if i == 0 else Scope.FAMILY, title=f"EU RoHS CoC {v}",
                note="wzorzec URL Microchip (material-compliance-documents)"))
        result.manual_urls.append(self.PMC_PAGE)


class OnsemiAdapter(BaseAdapter):
    """onsemi: certyfikat RoHS+REACH i deklaracja składu generowane per OPN przez stały endpoint."""

    key = "onsemi"
    COC = "https://www.onsemi.com/PowerSolutions/MaterialComposition.do?export=coc_pdf&opnId={mpn}"
    MCD = "https://www.onsemi.com/PowerSolutions/MaterialComposition.do?export=pdf&opnId={mpn}"

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        q = self.q(mpn)
        result.add(Candidate(url=self.COC.format(mpn=q), doc_types={DocType.ROHS, DocType.REACH},
                             scope=Scope.PART, title=f"Certificate of Compliance RoHS and REACH {mpn}"))
        result.add(Candidate(url=self.MCD.format(mpn=q), doc_types={DocType.MCD},
                             scope=Scope.PART, title=f"Material Composition Declaration {mpn}"))


class NexperiaAdapter(BaseAdapter):
    """Nexperia: strona chemical content per typ: nexperia.com/chemical-content/{type}.html
    (deklaracja składu z informacją RoHS / REACH; zawiera link do wersji PDF).
    Numery zamówieniowe typu 'BAS16,215' / 'BAS16.215' są sprowadzane do numeru typu.
    """

    key = "nexperia"
    PAGE = "https://www.nexperia.com/chemical-content/{ptn}.html"

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        variants = [mpn.strip()]
        type_no = re.split(r"[,.]\d{3}$", mpn.strip())[0]
        if type_no != variants[0]:
            variants.append(type_no)
        for v in variants:
            url = self.PAGE.format(ptn=self.q(v))
            page = self.fetch_html(url, ctx, result)
            if not page:
                continue
            soup, final = page
            n = self.collect_document_links(soup, final, mpn, ctx, result,
                                            default_types={DocType.MCD, DocType.ROHS, DocType.REACH})
            if not n:
                # Sama strona HTML jest deklaracją składu – zapisujemy jej kopię.
                result.add(Candidate(url=final, doc_types={DocType.MCD, DocType.ROHS, DocType.REACH},
                                     scope=Scope.PART if v == variants[0] else Scope.FAMILY,
                                     title=f"Chemical content {v}", allow_html=True,
                                     note="zapis strony HTML (brak linku do PDF)"))
            return


class NXPAdapter(BaseAdapter):
    """NXP: deklaracja składu (MCDS) per część pod nxp.com/mcds/{PN}.pdf oraz strona
    chemical content i strona części z certyfikatem RoHS (Certificate of Analysis)."""

    key = "nxp"
    MCDS = "https://www.nxp.com/mcds/{mpn}.pdf"
    CHEM = "https://www.nxp.com/webapp/chemical-content/{mpn}.html"
    PART = "https://www.nxp.com/webapp/search.partspiecedetail.framework?PART_NUMBER={mpn}"

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        q = self.q(mpn)
        result.add(Candidate(url=self.MCDS.format(mpn=q), doc_types={DocType.MCD},
                             scope=Scope.PART, title=f"Material Content Declaration {mpn}"))
        for tpl in (self.CHEM, self.PART):
            url = tpl.format(mpn=q)
            page = self.fetch_html(url, ctx, result)
            if page:
                soup, final = page
                self.collect_document_links(soup, final, mpn, ctx, result)
            else:
                result.manual_urls.append(url)


class InfineonAdapter(BaseAdapter):
    """Infineon (w tym IR, Cypress): strona produktu infineon.com/part/{name} zawiera
    Material Content Data Sheet (z deklaracją RoHS) w sekcji dokumentów."""

    key = "infineon"
    PART = "https://www.infineon.com/part/{mpn}"
    _MCDS = re.compile(r"material.?content|materialcontentsheet|mcds", re.IGNORECASE)

    def find_part_documents(self, mpn: str, ctx: AdapterContext, result: SearchResult) -> None:
        url = self.PART.format(mpn=self.q(mpn))
        page = self.fetch_html(url, ctx, result)
        if not page:
            result.manual_urls.append(url)
            return
        soup, final = page
        for link, text in self.iter_links(soup, final):
            if self._MCDS.search(link) or self._MCDS.search(text):
                result.add(Candidate(url=link, doc_types={DocType.MCD, DocType.ROHS},
                                     scope=Scope.PART, title=text or "Material Content Data Sheet",
                                     source_page=final))
        self.collect_document_links(soup, final, mpn, ctx, result)

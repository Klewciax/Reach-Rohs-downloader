"""Zaufani dystrybutorzy / agregatory jako ZAPASOWE źródło dokumentów i danych.

Obsługiwane (wyłącznie przez oficjalne API – strony WWW tych serwisów blokują boty
i zabraniają scrapingu w regulaminach):
  * Nexar / Octopart  – GraphQL, client credentials   (NEXAR_CLIENT_ID, NEXAR_CLIENT_SECRET)
  * DigiKey API v4    – OAuth2 client credentials     (DIGIKEY_CLIENT_ID, DIGIKEY_CLIENT_SECRET)
  * Mouser Search API – klucz API                     (MOUSER_API_KEY)
  * TME API           – token + sekret aplikacji      (TME_TOKEN, TME_APP_SECRET)

Klucze podaje się w zmiennych środowiskowych (lub `api_keys:` w pliku konfiguracyjnym).
Bez klucza dane źródło jest pomijane, co jest zapisywane w raporcie.

Zasady:
  * wynik z dystrybutora jest brany pod uwagę tylko, gdy MPN zgadza się DOKŁADNIE, a producent
    po stronie dystrybutora jest tym samym producentem co w BoM (para MPN + Manufacturer),
  * dokument pobrany od dystrybutora jest oznaczany w raporcie jako "Źródło: <dystrybutor>",
  * dokument, w którego treści nie występuje nazwa producenta, traktujemy jako oświadczenie
    dystrybutora / strony trzeciej – nie zastępuje deklaracji producenta.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from .classify import compact
from .http_client import FetchError, LoginRequired, PoliteSession

log = logging.getLogger(__name__)

# Dokumenty, które nas interesują (nazwa / tytuł / URL)
DOC_KEYWORDS = re.compile(r"rohs|reach|svhc|environment|compliance|material|declaration|certificate|"
                          r"conformity|green|halogen|ipc.?1752|mcd|lead.?free|eco", re.I)


@dataclass
class DistributorDoc:
    title: str
    url: str


@dataclass
class DistributorPart:
    source: str                     # "Nexar/Octopart", "DigiKey", "Mouser", "TME"
    mpn: str
    manufacturer: str
    manufacturer_homepage: str = ""
    datasheet_url: str = ""
    product_url: str = ""
    rohs_status: str = ""
    reach_status: str = ""
    lifecycle_status: str = ""
    documents: list[DistributorDoc] = field(default_factory=list)


class DistributorClient:
    name = "base"
    api_domains: list[str] = []
    #: domeny, z których wolno pobrać dokument wskazany przez to źródło (poza domenami producenta)
    doc_domains: list[str] = []
    env: tuple[str, ...] = ()

    def __init__(self, session: PoliteSession, keys: dict[str, str]):
        self.session = session
        self.keys = keys

    @classmethod
    def credentials(cls, config_keys: dict) -> dict[str, str] | None:
        vals = {}
        for var in cls.env:
            v = os.environ.get(var) or (config_keys or {}).get(var) or (config_keys or {}).get(var.lower())
            if not v:
                return None
            vals[var] = str(v)
        return vals

    def lookup(self, mpn: str) -> list[DistributorPart]:  # pragma: no cover - interfejs
        raise NotImplementedError


class _OAuthClient(DistributorClient):
    token_url = ""
    scope = ""
    _token: tuple[str, float] | None = None

    def token(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        cid, secret = (self.keys[v] for v in self.env)
        data = {"grant_type": "client_credentials", "client_id": cid, "client_secret": secret}
        if self.scope:
            data["scope"] = self.scope
        resp = self.session.api("POST", self.token_url, self.api_domains, data=data)
        body = resp.json()
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 600)))
        return self._token[0]


class NexarClient(_OAuthClient):
    name = "Nexar/Octopart"
    env = ("NEXAR_CLIENT_ID", "NEXAR_CLIENT_SECRET")
    api_domains = ["nexar.com"]
    doc_domains = ["octopart.com", "nexar.com"]
    token_url = "https://identity.nexar.com/connect/token"
    scope = "supply.domain"
    QUERY = """query($q: String!) {
  supSearchMpn(q: $q, limit: 5) {
    results { part {
      mpn
      manufacturer { name homepageUrl }
      bestDatasheet { url }
      documentCollections { name documents { name url } }
    } }
  }
}"""

    def lookup(self, mpn: str) -> list[DistributorPart]:
        resp = self.session.api("POST", "https://api.nexar.com/graphql", self.api_domains,
                                json={"query": self.QUERY, "variables": {"q": mpn}},
                                headers={"Authorization": f"Bearer {self.token()}"})
        data = resp.json()
        out = []
        for res in (((data.get("data") or {}).get("supSearchMpn") or {}).get("results") or []):
            part = res.get("part") or {}
            man = part.get("manufacturer") or {}
            docs = []
            for coll in part.get("documentCollections") or []:
                for d in coll.get("documents") or []:
                    if d.get("url"):
                        docs.append(DistributorDoc(f"{coll.get('name', '')}: {d.get('name', '')}", d["url"]))
            out.append(DistributorPart(self.name, part.get("mpn", ""), man.get("name", ""),
                                       man.get("homepageUrl") or "", (part.get("bestDatasheet") or {}).get("url") or "",
                                       documents=docs))
        return out


class DigiKeyClient(_OAuthClient):
    name = "DigiKey"
    env = ("DIGIKEY_CLIENT_ID", "DIGIKEY_CLIENT_SECRET")
    api_domains = ["api.digikey.com"]
    doc_domains = ["digikey.com"]
    token_url = "https://api.digikey.com/v1/oauth2/token"
    BASE = "https://api.digikey.com/products/v4/search"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token()}", "X-DIGIKEY-Client-Id": self.keys["DIGIKEY_CLIENT_ID"],
                "X-DIGIKEY-Locale-Language": "en"}

    def lookup(self, mpn: str) -> list[DistributorPart]:
        q = quote(mpn, safe="")
        try:
            resp = self.session.api("GET", f"{self.BASE}/{q}/productdetails", self.api_domains, headers=self._headers())
        except FetchError as exc:
            if exc.status == 404:
                return []
            raise
        prod = (resp.json() or {}).get("Product") or {}
        if not prod:
            return []
        cls = prod.get("Classifications") or {}
        part = DistributorPart(self.name, prod.get("ManufacturerProductNumber", ""),
                               (prod.get("Manufacturer") or {}).get("Name", ""),
                               datasheet_url=prod.get("DatasheetUrl") or "", product_url=prod.get("ProductUrl") or "",
                               rohs_status=cls.get("RohsStatus") or "", reach_status=cls.get("ReachStatus") or "",
                               lifecycle_status=(prod.get("ProductStatus") or {}).get("Status")
                               or (prod.get("ProductStatus") or {}).get("Text") or "")
        try:
            media = self.session.api("GET", f"{self.BASE}/{q}/media", self.api_domains, headers=self._headers()).json()
            for m in (media or {}).get("MediaLinks") or []:
                if m.get("Url"):
                    part.documents.append(DistributorDoc(f"{m.get('MediaType', '')}: {m.get('Title', '')}",
                                                         _abs(m["Url"])))
        except FetchError as exc:
            log.info("DigiKey media %s: %s", mpn, exc)
        return [part]


class MouserClient(DistributorClient):
    """Mouser Search API zwraca status RoHS, status cyklu życia i link do karty katalogowej
    (bez dokumentów zgodności) – służy jako dodatkowa informacja i do wykrywania domeny producenta."""
    name = "Mouser"
    env = ("MOUSER_API_KEY",)
    api_domains = ["api.mouser.com"]
    doc_domains = ["mouser.com"]

    def lookup(self, mpn: str) -> list[DistributorPart]:
        url = f"https://api.mouser.com/api/v1/search/partnumber?apiKey={self.keys['MOUSER_API_KEY']}"
        body = {"SearchByPartRequest": {"mouserPartNumber": mpn, "partSearchOptions": "Exact"}}
        data = self.session.api("POST", url, self.api_domains, json=body).json() or {}
        if data.get("Errors"):
            raise FetchError(url.split("?")[0], f"Mouser API: {data['Errors']}")
        out = []
        for p in ((data.get("SearchResults") or {}).get("Parts") or []):
            out.append(DistributorPart(self.name, p.get("ManufacturerPartNumber", ""), p.get("Manufacturer", ""),
                                       datasheet_url=p.get("DataSheetUrl") or "",
                                       product_url=p.get("ProductDetailUrl") or "",
                                       rohs_status=p.get("ROHSStatus") or "",
                                       lifecycle_status=p.get("LifecycleStatus") or ""))
        return out


def tme_signature(method: str, url: str, params: dict[str, str], secret: str) -> str:
    """Podpis TME API (jak OAuth 1.0a HMAC-SHA1): METHOD&enc(url)&enc(posortowane parametry)."""
    enc = lambda s: quote(str(s), safe="~")  # noqa: E731
    normalized = "&".join(f"{enc(k)}={enc(v)}" for k, v in sorted(params.items()))
    base = "&".join([method.upper(), enc(url), enc(normalized)])
    digest = hmac.new(secret.encode(), base.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class TMEClient(DistributorClient):
    name = "TME"
    env = ("TME_TOKEN", "TME_APP_SECRET")
    api_domains = ["api.tme.eu"]
    doc_domains = ["tme.eu", "tme.com"]
    BASE = "https://api.tme.eu"

    def _call(self, action: str, params: dict[str, str]) -> dict:
        url = f"{self.BASE}/{action}.json"
        p = {"Token": self.keys["TME_TOKEN"], "Country": "PL", "Language": "EN", **params}
        p["ApiSignature"] = tme_signature("POST", url, p, self.keys["TME_APP_SECRET"])
        data = self.session.api("POST", url, self.api_domains, data=p).json() or {}
        if data.get("Status") not in (None, "OK"):
            raise FetchError(url, f"TME API: {data.get('Status')} {data.get('Error', '')}")
        return data.get("Data") or {}

    def lookup(self, mpn: str) -> list[DistributorPart]:
        found = self._call("Products/Search", {"SearchPlain": mpn})
        products = [p for p in found.get("ProductList") or []
                    if compact(p.get("OriginalSymbol", "")) == compact(mpn)][:3]
        if not products:
            return []
        params = {f"SymbolList[{i}]": p["Symbol"] for i, p in enumerate(products)}
        files = {f.get("Symbol"): f for f in (self._call("Products/GetProductsFiles", params).get("ProductList") or [])}
        out = []
        for p in products:
            part = DistributorPart(self.name, p.get("OriginalSymbol", ""), p.get("Producer", ""),
                                   product_url=_abs(p.get("ProductInformationPage", "")))
            for d in ((files.get(p["Symbol"]) or {}).get("Files") or {}).get("DocumentList") or []:
                if d.get("DocumentUrl"):
                    part.documents.append(DistributorDoc(f"{d.get('DocumentType', '')}: "
                                                         f"{d.get('DocumentName') or d.get('Filename', '')}",
                                                         _abs(d["DocumentUrl"])))
            out.append(part)
        return out


def _abs(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


CLIENTS: dict[str, type[DistributorClient]] = {
    "nexar": NexarClient, "octopart": NexarClient, "digikey": DigiKeyClient, "mouser": MouserClient, "tme": TMEClient,
}


class DistributorHub:
    """Zarządza klientami, cache'uje wyniki per MPN, raportuje brakujące klucze."""

    def __init__(self, session: PoliteSession, sources: list[str], config_keys: dict | None = None):
        self.clients: list[DistributorClient] = []
        self.missing_keys: list[str] = []
        self.errors: dict[str, str] = {}
        seen = set()
        for s in sources:
            cls = CLIENTS.get(s.lower())
            if cls is None or cls in seen:
                continue
            seen.add(cls)
            keys = cls.credentials(config_keys or {})
            if keys is None:
                self.missing_keys.append(f"{cls.name} (zmienne: {', '.join(cls.env)})")
            else:
                self.clients.append(cls(session, keys))
        self._cache: dict[str, list[DistributorPart]] = {}

    @property
    def active(self) -> bool:
        return bool(self.clients)

    def lookup(self, mpn: str) -> list[DistributorPart]:
        key = compact(mpn)
        if key in self._cache:
            return self._cache[key]
        out: list[DistributorPart] = []
        for c in self.clients:
            if c.name in self.errors and "logowania" in self.errors[c.name]:
                continue  # zły klucz – nie ponawiamy przy każdym MPN
            try:
                out += c.lookup(mpn)
            except LoginRequired as exc:
                self.errors[c.name] = str(exc)
                log.warning("%s: %s", c.name, exc)
            except (FetchError, ValueError, KeyError) as exc:
                log.info("%s: brak danych dla %s: %s", c.name, mpn, exc)
        self._cache[key] = out
        return out

    def doc_domains(self, source: str) -> list[str]:
        for c in self.clients:
            if c.name == source:
                return list(c.doc_domains)
        return []


def registrable_domain(url_or_host: str) -> str:
    host = (urlsplit(url_or_host).hostname if "/" in url_or_host else url_or_host) or ""
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "ac", "or", "ne", "net", "org", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])

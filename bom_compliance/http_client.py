"""Uprzejmy klient HTTP: whitelist domen, robots.txt, limit tempa, retry z backoffem.

Każde zapytanie (łącznie z każdym przekierowaniem) jest sprawdzane pod kątem
domeny producenta, więc narzędzie nigdy nie pobiera niczego spoza oficjalnych
domen — nawet jeśli serwer przekieruje do dystrybutora/agregatora.
"""
from __future__ import annotations

import logging
import random
import re
import time
import urllib.robotparser
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from .config import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}

# Wzorce URL typowe dla stron logowania (SSO, myTI, myMicrochip itd.)
LOGIN_URL_RE = re.compile(
    r"(/login|/log-in|/signin|/sign-in|/sso/|/oauth|/saml|/auth/|/account/log|"
    r"//login\.|//sso\.|//signin\.|//auth\.|//account\.|//idp\.)",
    re.IGNORECASE,
)


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: int | None = None):
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


class DomainNotAllowed(FetchError):
    pass


class RobotsDisallowed(FetchError):
    pass


class LoginRequired(FetchError):
    pass


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def host_allowed(url_or_host: str, domains: list[str]) -> bool:
    """True, jeśli host jest jedną z domen producenta lub jej subdomeną."""
    host = host_of(url_or_host) if "/" in url_or_host else url_or_host.lower()
    if not host:
        return False
    for d in domains:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class PoliteSession:
    def __init__(self, settings: Settings, sleep: Callable[[float], None] = time.sleep):
        self.settings = settings
        self._sleep = sleep
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
            }
        )
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._dead_hosts: dict[str, str] = {}  # host -> powód (nie odpowiada; pomijany w tym przebiegu)
        # Cache stron HTML w obrębie jednego przebiegu (wypełniany przez adaptery).
        self.page_cache: dict[str, tuple[str, str, str]] = {}

    # ------------------------------------------------------------------ tempo
    def _delay_for(self, host: str) -> float:
        delay = self.settings.min_delay_per_host
        rp = self._robots.get(host)
        if rp is not None:
            try:
                cd = rp.crawl_delay(self.settings.user_agent)
            except Exception:  # pragma: no cover - defensywnie
                cd = None
            if cd:
                delay = max(delay, float(cd))
        return delay

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self._delay_for(host) + random.uniform(0, self.settings.delay_jitter) - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = time.monotonic()

    # ----------------------------------------------------------------- robots
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host in self._robots:
            return self._robots[host]
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser(robots_url)
        quick = (min(self.settings.connect_timeout, 5.0), self.settings.robots_timeout)
        allow_on_error = self.settings.robots_unreachable_policy.lower() != "disallow"
        try:
            resp = self._raw_request("GET", robots_url, host, timeout=quick, retries=0)
            for _ in range(5):  # RFC 9309: śledź do 5 przekierowań
                if resp.status_code not in (301, 302, 303, 307, 308) or not resp.headers.get("Location"):
                    break
                nxt = urljoin(robots_url, resp.headers["Location"])
                resp = self._raw_request("GET", nxt, host_of(nxt), timeout=quick, retries=0)
        except FetchError as exc:
            # Brak odpowiedzi na robots.txt to nie zakaz – większość serwisów nie ma robots.txt albo
            # odpowiada wolno. Jawne reguły Disallow są respektowane zawsze, gdy plik da się pobrać.
            log.info("robots.txt dla %s niedostępny (%s) – %s", host, exc,
                     "brak ograniczeń" if allow_on_error else "traktuję jako zakaz (robots_unreachable_policy)")
            rp.allow_all, rp.disallow_all = allow_on_error, not allow_on_error
            self._robots[host] = rp
            return rp
        if resp.status_code == 200 and "html" not in resp.headers.get("Content-Type", "").lower():
            rp.parse(resp.text.splitlines())
        elif resp.status_code == 200 or 400 <= resp.status_code < 500:
            rp.allow_all = True  # brak robots.txt (lub strona HTML zamiast pliku) -> brak ograniczeń
        else:
            rp.allow_all, rp.disallow_all = allow_on_error, not allow_on_error
        self._robots[host] = rp
        return rp

    def robots_allows(self, url: str) -> bool:
        if not self.settings.respect_robots:
            return True
        rp = self._robots_for(url)
        return rp is None or rp.can_fetch(self.settings.user_agent, url)

    # ------------------------------------------------------------------- HTTP
    def _raw_get(self, url: str, host: str, stream: bool = False) -> requests.Response:
        return self._raw_request("GET", url, host, stream=stream)

    def _raw_request(self, method: str, url: str, host: str, stream: bool = False, timeout=None, retries=None,
                     **kwargs) -> requests.Response:
        """Zapytanie z limitem tempa i retry (bez obsługi przekierowań)."""
        s = self.settings
        if s.skip_dead_hosts and host in self._dead_hosts:
            raise FetchError(url, f"Host {host} nie odpowiadał wcześniej w tym przebiegu ({self._dead_hosts[host]}) – pominięto")
        max_retries = s.max_retries if retries is None else retries
        timeout = timeout or (s.connect_timeout, s.read_timeout)
        last_error: str = "nieznany błąd"
        network_failure = False
        for attempt in range(max_retries + 1):
            self._throttle(host)
            try:
                resp = self._session.request(
                    method,
                    url,
                    timeout=timeout,
                    allow_redirects=False,
                    stream=stream,
                    **kwargs,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"błąd sieci: {exc.__class__.__name__}"
                network_failure = True
                wait = min(s.backoff_max, s.backoff_base * 2 ** attempt)
            else:
                network_failure = False
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                wait = _parse_retry_after(resp.headers.get("Retry-After"))
                if wait is None:
                    wait = s.backoff_base * 2 ** attempt
                wait = min(s.backoff_max, wait)
                resp.close()
            if attempt < max_retries:
                wait += random.uniform(0, s.delay_jitter)
                log.info("Ponawiam %s za %.1fs (%s, próba %d/%d)", url, wait, last_error, attempt + 1, max_retries)
                self._sleep(wait)
        if network_failure and s.skip_dead_hosts:
            self._dead_hosts[host] = last_error
            log.warning("Host %s nie odpowiada (%s) – pomijam go do końca przebiegu", host, last_error)
        raise FetchError(url, f"Nie udało się pobrać po {max_retries + 1} próbach: {last_error}")

    def get(self, url: str, domains: list[str], stream: bool = False) -> requests.Response:
        """GET ograniczony do domen producenta; ręcznie śledzi przekierowania."""
        current = url
        for _ in range(self.settings.max_redirects + 1):
            if LOGIN_URL_RE.search(current) and current != url:
                raise LoginRequired(current, "Przekierowanie do strony logowania")
            if not current.lower().startswith(("http://", "https://")):
                raise FetchError(current, "Nieobsługiwany schemat URL")
            if not host_allowed(current, domains):
                raise DomainNotAllowed(current, f"Domena spoza listy producenta {domains}")
            if not self.robots_allows(current):
                raise RobotsDisallowed(current, "Zablokowane przez robots.txt (jawny zakaz Disallow)")
            resp = self._raw_get(current, host_of(current), stream=stream)
            if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    raise FetchError(current, "Przekierowanie bez nagłówka Location", resp.status_code)
                current = urljoin(current, location)
                continue
            if resp.status_code == 401:
                resp.close()
                raise LoginRequired(current, "Wymagane uwierzytelnienie (HTTP 401)", 401)
            if resp.status_code >= 400:
                resp.close()
                raise FetchError(current, f"HTTP {resp.status_code}", resp.status_code)
            return resp
        raise FetchError(url, "Zbyt wiele przekierowań")

    def api(self, method: str, url: str, domains: list[str], **kwargs) -> requests.Response:
        """Wywołanie oficjalnego API (dystrybutora): bez robots.txt (API jest przeznaczone do
        dostępu programowego), z limitem tempa, retry i ograniczeniem do domeny API."""
        if not host_allowed(url, domains):
            raise DomainNotAllowed(url, f"Domena API spoza listy {domains}")
        resp = self._raw_request(method, url, host_of(url), **kwargs)
        if resp.status_code in (401, 403):
            body = " ".join((resp.text or "").split())[:300]
            resp.close()
            raise LoginRequired(url, f"API odrzuciło dostęp (HTTP {resp.status_code})"
                                     + (f"; odpowiedź serwera: {body}" if body else ""), resp.status_code)
        if resp.status_code >= 400:
            body = resp.text[:200]
            resp.close()
            raise FetchError(url, f"HTTP {resp.status_code}: {body}", resp.status_code)
        return resp

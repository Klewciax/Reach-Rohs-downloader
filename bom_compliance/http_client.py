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
                "Accept": "text/html,application/pdf,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._robots_unreachable: set[str] = set()
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
        try:
            resp = self._raw_get(robots_url, host)
            for _ in range(5):  # RFC 9309: śledź do 5 przekierowań
                if resp.status_code not in (301, 302, 303, 307, 308) or not resp.headers.get("Location"):
                    break
                nxt = urljoin(robots_url, resp.headers["Location"])
                resp = self._raw_get(nxt, host_of(nxt))
        except FetchError as exc:
            # RFC 9309: robots.txt nieosiągalny -> zakładamy pełny zakaz.
            log.warning("robots.txt niedostępny dla %s: %s", host, exc)
            rp.disallow_all = True
            self._robots_unreachable.add(host)
            self._robots[host] = rp
            return rp
        if resp.status_code == 200:
            rp.parse(resp.text.splitlines())
        elif 400 <= resp.status_code < 500:
            rp.allow_all = True  # RFC 9309: brak robots.txt -> brak ograniczeń
        else:
            rp.disallow_all = True
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

    def _raw_request(self, method: str, url: str, host: str, stream: bool = False, **kwargs) -> requests.Response:
        """Zapytanie z limitem tempa i retry (bez obsługi przekierowań)."""
        s = self.settings
        last_error: str = "nieznany błąd"
        for attempt in range(s.max_retries + 1):
            self._throttle(host)
            try:
                resp = self._session.request(
                    method,
                    url,
                    timeout=(s.connect_timeout, s.read_timeout),
                    allow_redirects=False,
                    stream=stream,
                    **kwargs,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"błąd sieci: {exc.__class__.__name__}"
                wait = min(s.backoff_max, s.backoff_base * 2 ** attempt)
            else:
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                last_error = f"HTTP {resp.status_code}"
                wait = _parse_retry_after(resp.headers.get("Retry-After"))
                if wait is None:
                    wait = s.backoff_base * 2 ** attempt
                wait = min(s.backoff_max, wait)
                resp.close()
            if attempt < s.max_retries:
                wait += random.uniform(0, s.delay_jitter)
                log.info("Ponawiam %s za %.1fs (%s, próba %d/%d)", url, wait, last_error, attempt + 1, s.max_retries)
                self._sleep(wait)
        raise FetchError(url, f"Nie udało się pobrać po {s.max_retries + 1} próbach: {last_error}")

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
                host = host_of(current)
                if host in self._robots_unreachable:
                    raise FetchError(current, f"Błąd sieci: nie można pobrać robots.txt dla {host} "
                                              "(zgodnie z RFC 9309 pobieranie wstrzymane)")
                raise RobotsDisallowed(current, "Zablokowane przez robots.txt")
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

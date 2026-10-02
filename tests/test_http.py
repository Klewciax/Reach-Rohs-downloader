import pytest
import responses

from bom_compliance.http_client import (
    DomainNotAllowed, FetchError, LoginRequired, PoliteSession, RobotsDisallowed, host_allowed,
)

D = ["example-mfr.com"]


def test_host_allowed():
    assert host_allowed("https://www.example-mfr.com/x.pdf", D)
    assert host_allowed("https://docs.example-mfr.com/x.pdf", D)
    assert not host_allowed("https://example-mfr.com.evil.net/x.pdf", D)
    assert not host_allowed("https://notexample-mfr.com/x.pdf", D)
    assert not host_allowed("https://www.mouser.com/x.pdf", D)


@responses.activate
def test_robots_disallow(settings):
    responses.get("https://www.example-mfr.com/robots.txt", body="User-agent: *\nDisallow: /private/\n")
    s = PoliteSession(settings, sleep=lambda _: None)
    with pytest.raises(RobotsDisallowed):
        s.get("https://www.example-mfr.com/private/doc.pdf", D)


@responses.activate
def test_redirect_to_distributor_blocked(settings):
    responses.get("https://www.example-mfr.com/robots.txt", status=404)
    responses.get("https://www.example-mfr.com/doc.pdf", status=302,
                  headers={"Location": "https://www.distributor.com/doc.pdf"})
    s = PoliteSession(settings, sleep=lambda _: None)
    with pytest.raises(DomainNotAllowed):
        s.get("https://www.example-mfr.com/doc.pdf", D)
    assert not any("distributor" in c.request.url for c in responses.calls)


@responses.activate
def test_redirect_to_login(settings):
    responses.get("https://www.example-mfr.com/robots.txt", status=404)
    responses.get("https://www.example-mfr.com/doc.pdf", status=302,
                  headers={"Location": "https://login.example-mfr.com/sso?next=/doc.pdf"})
    s = PoliteSession(settings, sleep=lambda _: None)
    with pytest.raises(LoginRequired):
        s.get("https://www.example-mfr.com/doc.pdf", D)


@responses.activate
def test_retry_on_429_then_success(settings):
    sleeps = []
    responses.get("https://www.example-mfr.com/robots.txt", status=404)
    responses.get("https://www.example-mfr.com/doc.pdf", status=429, headers={"Retry-After": "7"})
    responses.get("https://www.example-mfr.com/doc.pdf", status=503)
    responses.get("https://www.example-mfr.com/doc.pdf", body=b"%PDF-1.4 ok")
    s = PoliteSession(settings, sleep=sleeps.append)
    r = s.get("https://www.example-mfr.com/doc.pdf", D)
    assert r.content.startswith(b"%PDF")
    assert 7.0 in sleeps  # Retry-After respektowany


@responses.activate
def test_retry_exhausted(settings):
    responses.get("https://www.example-mfr.com/robots.txt", status=404)
    responses.get("https://www.example-mfr.com/doc.pdf", status=500)
    s = PoliteSession(settings, sleep=lambda _: None)
    with pytest.raises(FetchError):
        s.get("https://www.example-mfr.com/doc.pdf", D)
    assert len([c for c in responses.calls if c.request.url.endswith("doc.pdf")]) == settings.max_retries + 1


@responses.activate
def test_robots_unreachable_does_not_block_by_default(settings):
    # robots.txt nie odpowiada (5xx) -> to nie jest zakaz; dokument jest pobierany
    responses.get("https://www.example-mfr.com/robots.txt", status=503)
    responses.get("https://www.example-mfr.com/doc.pdf", body=b"%PDF-1.4 ok")
    s = PoliteSession(settings, sleep=lambda _: None)
    assert s.get("https://www.example-mfr.com/doc.pdf", D).content.startswith(b"%PDF")
    robots_calls = [c for c in responses.calls if c.request.url.endswith("robots.txt")]
    assert len(robots_calls) == 1  # robots.txt bez ponowień


@responses.activate
def test_robots_unreachable_strict_policy(settings):
    settings.robots_unreachable_policy = "disallow"
    responses.get("https://www.example-mfr.com/robots.txt", status=503)
    s = PoliteSession(settings, sleep=lambda _: None)
    with pytest.raises(RobotsDisallowed):
        s.get("https://www.example-mfr.com/doc.pdf", D)


@responses.activate
def test_html_instead_of_robots_means_no_rules(settings):
    responses.get("https://www.example-mfr.com/robots.txt", body="<html>Disallow: / </html>",
                  content_type="text/html")
    responses.get("https://www.example-mfr.com/doc.pdf", body=b"%PDF-1.4 ok")
    s = PoliteSession(settings, sleep=lambda _: None)
    assert s.get("https://www.example-mfr.com/doc.pdf", D).status_code == 200


def test_dead_host_is_skipped_after_network_failure(settings):
    import requests as rq
    calls = []
    s = PoliteSession(settings, sleep=lambda _: None)

    def boom(*a, **k):
        calls.append(a[1])
        raise rq.ConnectTimeout("timeout")
    s._session.request = boom
    with pytest.raises(FetchError):
        s.get("https://www.slow-mfr.com/a.html", ["slow-mfr.com"])
    n = len(calls)
    with pytest.raises(FetchError, match="nie odpowiadał"):
        s.get("https://www.slow-mfr.com/b.html", ["slow-mfr.com"])
    assert len(calls) == n  # brak kolejnych prób połączenia z martwym hostem

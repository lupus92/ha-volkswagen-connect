"""Self-check: VW's v2 marketing-consent page must not break the portal login (#34).

Run inside the HA container (needs aiohttp + bs4): python3 tests/test_portal_marketing_consent.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "custom_components/volkswagen_connect/website_portal.py"
_spec = importlib.util.spec_from_file_location("website_portal", _SRC)
wp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wp)

IDENTITY = "https://identity.vwgroup.io"
MARKETING = f"{IDENTITY}/v2/login/ui/marketing-consent?relayState=rs-jwt&state=st-1"
SKIP = f"{IDENTITY}/v2/login/ui/marketing-consent/skip"
POST_LOGIN = f"{IDENTITY}/v2/api/flow-state/post-login?relayState=rs-2"
CONTINUE = f"{IDENTITY}/continue?state=st-1"
CODE = "https://www.volkswagen.de/app/authproxy/login/oauth2/code/vw-de?code=c"
PORTAL_OK = "https://www.volkswagen.de/de/besitzer-und-nutzer/myvolkswagen.html"
MFA = f"{IDENTITY}/u/mfa-email-challenge?state=m"
MFA_FORM = '<form action=""><input name="state" value="m"><input name="code"></form>'

DATA = {
    "page": "marketingConsent",
    "baseUrl": f"{IDENTITY}/v2",
    "content": "<p>Ja, ich möchte {braces} in Texten nicht stolpern</p>",
    "channels": [
        {"channelId": "email", "channelType": "BOUND_TO_BASIC_AGREEMENT"},
        {"channelId": "mail", "channelType": "NOT_USED"},
        {"channelId": "app", "channelType": "BOUND_TO_BASIC_AGREEMENT"},
    ],
    "relayStateToken": "rs-jwt",
    "state": "st-1",
    "mdKey": "VW-MEV",
    "mdLanguage": "en",
}
PAGE = (
    "<html><head><title>marketingConsent</title></head><body><div id=root></div>"
    f"<script>\n window._IDK = {json.dumps({'Data': DATA})};\n</script>"
    '<script src="https://identity.vwgroup.io/v2/public/web.js"></script></body></html>'
)


class _Response:
    def __init__(self, status: int, url: str, location: str | None = None, body: str = "") -> None:
        self.status = status
        self.url = url
        self.headers = {"Location": location} if location else {}
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """VW's identity host with the marketing page in the way, as seen hop by hop."""

    def __init__(self, first_landing: str = MARKETING) -> None:
        self.first_landing = first_landing
        self.after_skip = POST_LOGIN
        self.posts: list[tuple[str, dict]] = []
        self.cookie_jar = ()

    def get(self, url: str, allow_redirects: bool = True, **_):
        if url == wp.AUTHPROXY_LOGIN and allow_redirects:
            if self.first_landing == MARKETING:
                return _Response(200, MARKETING, body=PAGE)
            return _Response(200, self.first_landing, body="<form></form>")
        if url == MARKETING:
            return _Response(200, MARKETING, body=PAGE)
        if url == MFA:
            return _Response(200, MFA, body=MFA_FORM)
        hops = {POST_LOGIN: CONTINUE, CONTINUE: CODE, CODE: PORTAL_OK}
        if url in hops:
            return _Response(302, url, location=hops[url])
        return _Response(200, url)

    def post(self, url: str, data=None, **_):
        self.posts.append((url, dict(data or {})))
        if url == SKIP:
            return _Response(303, url, location=self.after_skip)
        if url.startswith(f"{IDENTITY}/u/login"):
            return _Response(302, url, location=MFA)
        if url.startswith(MFA.split("?")[0]):
            return _Response(302, url, location=MARKETING)
        return _Response(400, url)


def _skips(session: _Session) -> list[dict]:
    return [form for url, form in session.posts if url == SKIP]


def test_skip_form_mirrors_the_not_now_button() -> None:
    action, form = wp._marketing_consent_skip(MARKETING, PAGE)
    assert action == SKIP, action
    assert form == {
        "channelemail": "false",
        "channelmail": "not_used",
        "channelapp": "false",
        "state": "st-1",
        "relayState": "rs-jwt",
        "mdKey": "VW-MEV",
        "mdLanguage": "en",
    }, form
    assert "true" not in form.values()


def test_only_the_marketing_page_is_skipped() -> None:
    assert wp._marketing_consent_skip(PORTAL_OK, PAGE) is None
    assert wp._marketing_consent_skip(f"{IDENTITY}/v2/login/ui/consent", PAGE) is None
    assert wp._marketing_consent_skip(MARKETING, "<html></html>") is None
    assert wp._marketing_consent_skip(MARKETING, "window._IDK = {broken") is None


def test_silent_refresh_heals_through_the_marketing_page() -> None:
    session = _Session()
    client = wp.WebsitePortalClient(session, "a@b.c", "pw")
    asyncio.run(client.refresh())
    assert len(_skips(session)) == 1, session.posts


def test_silent_login_heals_through_the_marketing_page() -> None:
    session = _Session()
    client = wp.WebsitePortalClient(session, "a@b.c", "pw")
    assert asyncio.run(client.begin_login()) == "ok"
    assert len(_skips(session)) == 1, session.posts


def test_code_wanted_after_the_marketing_page_asks_for_it() -> None:
    session = _Session()
    session.after_skip = MFA
    client = wp.WebsitePortalClient(session, "a@b.c", "pw")
    try:
        asyncio.run(client.refresh())
        raise AssertionError("expected reauth")
    except wp.WebsitePortalOtpRequired:
        pass
    client = wp.WebsitePortalClient(session, "a@b.c", "pw")
    assert asyncio.run(client.begin_login()) == "otp_required"
    assert client._mfa["page_url"] == MFA


def test_accepted_otp_is_not_reported_as_rejected() -> None:
    session = _Session(first_landing=f"{IDENTITY}/u/login?state=s")
    client = wp.WebsitePortalClient(session, "a@b.c", "pw")
    assert asyncio.run(client.begin_login()) == "otp_required"
    assert asyncio.run(client.submit_otp("123456")) == "ok"
    assert len(_skips(session)) == 1, session.posts


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")

"""Tools for verifying legal sources: EUR-Lex (CELLAR), Dziennik Ustaw, and generic pages."""

import ipaddress
import re
import socket
import xml.etree.ElementTree as ET
from typing import Annotated

import httpx
from pydantic import Field

from mistralai import agents

USER_AGENT = "energy-law-agent/1.0 (legal research; contact: workspace user)"
EURLEX_URL_LANG = {"eng": "EN", "pol": "PL", "deu": "DE", "fra": "FR", "ces": "CS"}


def _assert_public_host(url: str) -> None:
    host = httpx.URL(url).host
    if not host:
        raise agents.ToolError(f"Invalid URL, no host: {url}")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise agents.ToolError(f"Cannot resolve host {host}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise agents.ToolError(
                f"Refused: {host} resolves to a private, loopback or link-local address."
            )


def _get(url: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
    _assert_public_host(url)
    try:
        response = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            timeout=60,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise agents.ToolError(f"Request failed: {exc}") from exc
    if response.status_code >= 400:
        raise agents.ToolError(f"HTTP {response.status_code} for {response.url}")
    return response


def _text(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    value = "".join(element.itertext()).strip()
    return value or None


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].upper()


def _clean_title(title: str) -> str:
    title = re.sub(r"(\(?Text with EEA relevance\)?)(?=\1)", "", title).strip()
    return re.sub(r"\s+", " ", title)


@agents.tool
async def get_eur_lex_act(
    celex: Annotated[str, Field(description="CELEX number, e.g. '32023L2413', 'CELEX:32019R0943' or a full EUR-Lex URL.")],
    language: Annotated[str, Field(description="Preferred title language: 'eng', 'pol', 'deu', 'fra' or 'ces'.")] = "eng",
) -> dict:
    """Fetch official metadata of an EU legal act from the Publications Office (CELLAR) by its CELEX number.

    Returns the act's title in the requested language, key dates (document, signature,
    OJ publication, entry into force, transposition deadline for directives),
    in-force status, and the EUR-Lex page URL. Use it to verify numbers, dates and
    consolidated versions before citing an act.
    """
    match = re.search(r"(3\d{4}[A-Z]\d{4}\(?[0-9A-Za-z()]*\)?|1\d{4}[A-Z]\d{4}\(?[0-9A-Za-z()]*\)?)", celex)
    if not match:
        raise agents.ToolError(
            f"'{celex}' does not look like a CELEX number (expected e.g. 32023L2413)."
        )
    celex_id = match.group(1)
    response = _get(
        f"http://publications.europa.eu/resource/celex/{celex_id}",
        headers={"Accept": "application/xml; notice=object"},
    )
    root = ET.fromstring(response.content)
    work = root.find("WORK")
    if work is None:
        raise agents.ToolError(f"No metadata found for CELEX {celex_id}.")

    dates: dict[str, str] = {}
    for element in work.iter():
        name = _local_name(element.tag)
        if name in {
            "DATE_DOCUMENT",
            "DATE_SIGNATURE",
            "DATE_TRANSPOSITION",
            "RESOURCE_LEGAL_DATE_ENTRY-INTO-FORCE",
            "OFFICIAL-JOURNAL-ACT_DATE_PUBLICATION",
            "RESOURCE_LEGAL_DATE_END-OF-VALIDITY",
        }:
            value = _text(element.find("VALUE"))
            if value and name not in dates:
                dates[name.lower()] = value
    in_force_element = next((e for e in work.iter() if _local_name(e.tag) == "RESOURCE_LEGAL_IN-FORCE"), None)
    oj = {
        "section": _text(work.find("RESOURCE_LEGAL_TYPE/VALUE")) or _text(work.find("TYPE/VALUE")),
        "number": _text(work.find("OFFICIAL-JOURNAL-ACT_NUMBER/VALUE")),
        "year": _text(work.find("OFFICIAL-JOURNAL-ACT_YEAR/VALUE")),
    }
    eli = _text(work.find("ELI/VALUE"))

    lang_code = language.lower()[:3]
    titles: list[dict[str, str]] = []
    expression_uris = [e.findtext("URI/VALUE") for e in work.findall("WORK_HAS_EXPRESSION")]
    if expression_uris:
        branch = _get(expression_uris[0], headers={"Accept": "application/xml; notice=branch"})
        number_match = re.search(r"(\d{4})[A-Z](\d{4})", celex_id)
        act_number = (
            f"{int(number_match.group(1))}/{int(number_match.group(2))}" if number_match else None
        )
        for expression in ET.fromstring(branch.content).iter("EXPRESSION"):
            title_element = expression.find("EXPRESSION_TITLE")
            value = _text(title_element.find("VALUE") if title_element is not None else None)
            lang = _text(expression.find("EXPRESSION_USES_LANGUAGE/OP-CODE"))
            if value:
                titles.append({"language": (lang or "?").lower(), "title": _clean_title(value),
                               "own_act": act_number is None or act_number in value})
    own_titles = [t for t in titles if t["own_act"]] or titles
    chosen = next((t for t in own_titles if t["language"] == lang_code), None) or (
        next((t for t in own_titles if t["language"] == "eng"), None)
        or (own_titles[0] if own_titles else None)
    )

    return {
        "celex": celex_id,
        "title": chosen["title"] if chosen else None,
        "title_language": chosen["language"] if chosen else None,
        "titles_available_in": sorted({t["language"] for t in titles if t["own_act"]}),
        "dates": dates,
        "official_journal": oj if all(oj.values()) else None,
        "eli": eli,
        "in_force": _text(in_force_element) if in_force_element is not None else None,
        "eur_lex_url": f"https://eur-lex.europa.eu/legal-content/{EURLEX_URL_LANG.get(lang_code, 'EN')}/TXT/?uri=CELEX%3A{celex_id}",
    }


@agents.tool
async def fetch_pl_act(
    year: Annotated[int, Field(description="Year of the Dziennik Ustaw issue, e.g. 2024.")],
    position: Annotated[int, Field(description="Position number ('poz.') within the issue, e.g. 1173.")],
) -> dict:
    """Fetch metadata of a Polish legal act from Dziennik Ustaw (dziennikustaw.gov.pl) by year and position.

    Returns the issue title, the act's PDF link, and the ELI identifier. Use it to
    verify Dz.U. references (year, position) of Polish acts such as Prawo
    energetyczne or ustawa o OZE.
    """
    url = f"https://dziennikustaw.gov.pl/DU/{year}/{position}"
    response = _get(url)
    html = response.text
    title_match = re.search(r"<title>(.*?)</title>", html, re.S)
    description_match = re.search(
        r'<meta[^>]+(?:name="description"|property="og:description")[^>]+content="([^"]*)"', html, re.I
    )
    pdf_links = sorted(set(re.findall(r'href="(/D\d+\.pdf|/DU/\d+/\d+/D\d+\.pdf)"', html)))
    return {
        "issue_title": title_match.group(1).strip() if title_match else None,
        "description": description_match.group(1).strip() if description_match else None,
        "pdf_urls": [f"https://dziennikustaw.gov.pl{link}" for link in pdf_links],
        "eli_url": url,
    }


@agents.tool
async def web_search(
    query: Annotated[str, Field(description="Web search query, e.g. 'RED III transposition Poland status'.")],
    limit: Annotated[int, Field(description="Number of results to return, 1-10.", ge=1, le=10)] = 5,
) -> list[dict[str, str]]:
    """Search the web and return result titles with URLs and snippets.

    Use it to locate official sources (EUR-Lex, ISAP, Dziennik Ustaw, URE, PSE,
    ENTSO-E, ACER, European Commission) before fetching them. No API key needed.
    """
    _assert_public_host("https://html.duckduckgo.com/html/")
    try:
        response = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": USER_AGENT},
            timeout=60,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise agents.ToolError(f"Search request failed: {exc}") from exc
    if response.status_code >= 400:
        raise agents.ToolError(f"Search returned HTTP {response.status_code}; try again later.")
    results: list[dict[str, str]] = []
    for url, raw_title in re.findall(r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', response.text):
        if url.startswith("//duckduckgo.com/l/?uddg="):
            match = re.search(r"uddg=([^&]+)", url)
            if match:
                from urllib.parse import unquote

                url = unquote(match.group(1))
        title = re.sub(r"<[^>]+>", "", raw_title).strip()
        if title and url.startswith("http"):
            results.append({"title": title, "url": url})
        if len(results) >= limit:
            break
    if not results:
        raise agents.ToolError("No results found; try a different query.")
    return results


@agents.tool
async def fetch_page(
    url: Annotated[str, Field(description="Public https URL, e.g. a URE, PSE, ENTSO-E, ACER or EUR-Lex page.")],
    max_chars: Annotated[int, Field(description="Maximum number of characters of page text to return.")] = 8000,
) -> str:
    """Fetch a public web page and return its readable text (title plus body text).

    Use it to read regulator, TSO and institution pages (URE, PSE, ENTSO-E, ACER,
    European Commission) or article texts. Refuses private and internal addresses.
    """
    if not url.startswith(("http://", "https://")):
        raise agents.ToolError("Only http(s) URLs are supported.")
    response = _get(url)
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "xml" not in content_type and "text" not in content_type:
        return f"[Non-text content: {content_type or 'unknown type'}, {len(response.content)} bytes at {response.url}]"
    html = response.text
    title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    body = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else ""
    text = f"{title}\n\n{body}" if title else body
    if len(text) > max_chars:
        text = text[:max_chars] + f" ... [truncated, {len(text)} characters total; fetch again with a higher max_chars if needed]"
    return text

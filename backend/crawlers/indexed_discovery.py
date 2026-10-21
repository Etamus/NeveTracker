"""Domain-scoped public search discovery, without prices inferred from snippets."""

import base64
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from crawlers.paged_discovery import STORE_HOSTS, _checked_html, _fetch_html_result, _same_store_url


SEARCH_ENGINES = {
    "bing": "https://www.bing.com/search",
    "duckduckgo": "https://html.duckduckgo.com/html/",
}


def _fetch_indexed_html(url, timeout, use_browser):
    if use_browser:
        return _fetch_html_result(url, timeout, use_browser=True)
    # TLS and HTTP/2 match a current browser. This avoids transport-only failures;
    # a CAPTCHA or HTTP denial remains a failure, never a successful empty search.
    from curl_cffi.requests import Session
    try:
        with Session(impersonate="chrome") as session:
            response = session.get(url, timeout=(5, timeout), headers={"Accept-Language": "pt-BR,pt;q=0.9"})
            if response.status_code != 200:
                status = "BLOCKED" if response.status_code in {202, 401, 403, 429} else "HTTP_ERROR"
                return None, status, f"HTTP {response.status_code}"
            if urlparse(response.url).hostname != urlparse(url).hostname:
                return None, "BLOCKED", "Buscador redirecionou para outro dominio"
            return _checked_html(response.text, response.url)
    except Exception as exc:
        return None, "TIMEOUT" if "timeout" in str(exc).lower() else "HTTP_ERROR", str(exc)[:200]


def indexed_search_url(engine, store, term):
    return SEARCH_ENGINES[engine] + "?" + urlencode({
        "q": f'"{term}" site:{STORE_HOSTS[store]}',
    })


def _destination(href, base, engine):
    absolute = urljoin(base, href or "")
    parsed = urlparse(absolute)
    host = parsed.hostname or ""
    query = parse_qs(parsed.query)
    if engine == "duckduckgo" and (host == "duckduckgo.com" or host.endswith(".duckduckgo.com")):
        return (query.get("uddg") or [absolute])[0]
    if engine == "bing" and (host == "bing.com" or host.endswith(".bing.com")):
        encoded = (query.get("u") or [""])[0]
        if encoded.startswith("a1"):
            try:
                value = encoded[2:]
                return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
            except (ValueError, UnicodeError):
                return ""
    return absolute


def _product_url(store, url):
    path = urlparse(url).path.lower()
    patterns = {
        "magalu": r"/p/[a-z0-9]+/",
        "amazon": r"/(?:dp|gp/product)/[a-z0-9]{10}",
        "mercado_livre": r"/(?:p/MLB\d+|up/MLBU\d+|MLB-?\d+)",
        "shopee": r"(?:-i\.\d+\.\d+|/product/\d+/\d+)",
    }
    if store in patterns:
        return bool(re.search(patterns[store], path, re.I))
    return bool(path.strip("/") and not re.search(r"/busca|/search|/catalogsearch|/loja/?$", path))


def fetch_indexed_page(engine, store, url, timeout=15, diagnostics=None, use_browser=False):
    fetched, status, reason = _fetch_indexed_html(url, timeout, use_browser)
    if diagnostics is not None:
        diagnostics.update(status=status, reason=reason)
    if not fetched:
        return [], None, False
    soup, final_url = fetched
    text = soup.get_text(" ", strip=True)
    if re.search(r"anomaly-modal|anomaly.js|select all squares|unusual traffic", str(soup), re.I):
        if diagnostics is not None:
            diagnostics.update(status="BLOCKED", reason="Buscador solicitou verificacao humana")
        return [], None, False
    cards = soup.select("li.b_algo") if engine == "bing" else soup.select(".result")
    items, seen = [], set()
    for card in cards:
        link = card.select_one("h2 a[href]" if engine == "bing" else ".result__a[href]")
        if not link:
            continue
        destination = _destination(link.get("href"), final_url, engine)
        destination = _same_store_url(final_url, destination, STORE_HOSTS[store])
        if not destination or not _product_url(store, destination) or destination in seen:
            continue
        seen.add(destination)
        title = link.get_text(" ", strip=True)
        seller_id = (parse_qs(urlparse(destination).query).get("seller_id") or [None])[0]
        item = {"name": title, "url": destination, "discovery_method": f"indexed_{engine}"}
        if seller_id:
            item["seller"] = seller_id + "-Magalu" if store == "magalu" else seller_id
        else:
            item["_seller_unknown"] = True
        items.append(item)
    if not items:
        empty = re.search(r"no results|nenhum resultado|n[aã]o foram encontrados resultados", text, re.I)
        # An HTTP 200 with irrelevant cards is not proof that this store has no stock.
        if not empty:
            if diagnostics is not None:
                diagnostics.update(status="SEARCH_MISMATCH", reason="Buscador nao retornou anuncios do dominio solicitado")
            return [], None, False
    next_url = None
    if engine == "bing":
        link = soup.select_one('a.sb_pagN[href], a[rel="next"][href]')
        if link:
            next_url = _same_store_url(final_url, link.get("href"), "bing.com")
    else:
        for form in soup.select("form"):
            button = form.select_one('input[value*="Next"], input[value*="Próxima"]')
            if button:
                params = {node.get("name"): node.get("value", "") for node in form.select('input[name]')}
                next_url = SEARCH_ENGINES[engine] + "?" + urlencode(params)
                break
    return items, next_url, True

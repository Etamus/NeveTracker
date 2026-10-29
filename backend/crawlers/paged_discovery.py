"""HTML discovery pages used by precise extraction; no marketplace API calls."""

import json
import re
from threading import BoundedSemaphore, Lock
from urllib.parse import parse_qs, quote, quote_plus, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from services.http_client import create_resilient_session, recover_public_html
from services.playwright_fallback import render_discovery_page
from services.product_resolver import resolve_candidate
from services.structured_products import number, offer_price, party_name, product_candidate, product_nodes, product_offers, types
from scrapers.specialty_stores import _decoded_template_soup

_domain_slots = {}
_domain_slots_lock = Lock()


def _request_slot(host):
    with _domain_slots_lock:
        return _domain_slots.setdefault(host, BoundedSemaphore(2))


STORE_HOSTS = {
    "electrolux": "electrolux.com.br",
    "midea": "midea.com.br",
    "consul": "consul.com.br",
    "brastemp": "brastemp.com.br",
    "amazon": "amazon.com.br",
    "mercado_livre": "mercadolivre.com.br",
    "magalu": "magazineluiza.com.br",
    "leroy_merlin": "leroymerlin.com.br",
    "shopee": "shopee.com.br",
    "dufrio": "dufrio.com.br",
    "friolar": "friolarpecas.com.br",
    "refrigeracao_mota": "refrigeracaomota.com.br",
    "mg_parts": "mgparts.com.br",
    "gold_service": "goldservice.com.br",
    "comclick": "comclick.com.br",
}

SEARCH_LABELS = {
    "electrolux": "Electrolux", "midea": "Midea", "consul": "Consul", "brastemp": "Brastemp",
    "leroy_merlin": "Leroy Merlin",
    "shopee": "Shopee",
    "dufrio": "Dufrio",
    "friolar": "Friolar",
    "refrigeracao_mota": "Refrigeração Mota",
    "mg_parts": "MG Parts",
    "gold_service": "Gold Service",
    "comclick": "ComClick",
}


def search_url(store, term):
    if store == "amazon":
        return f"https://www.amazon.com.br/s?k={quote_plus(term)}&s=price-asc-rank"
    if store == "mercado_livre":
        return f"https://lista.mercadolivre.com.br/{quote(term.strip().lower())}"
    if store == "magalu":
        return f"https://www.magazineluiza.com.br/busca/{quote(term)}/"
    search_templates = {
        "electrolux": "https://loja.electrolux.com.br/{term}?_q={term}&map=ft",
        "midea": "https://www.midea.com.br/s?q={term}",
        "consul": "https://www.consul.com.br/s?q={term}",
        "brastemp": "https://www.brastemp.com.br/s?q={term}",
        "leroy_merlin": "https://www.leroymerlin.com.br/busca?q={term}",
        "shopee": "https://shopee.com.br/search?keyword={term}",
        "dufrio": "https://www.dufrio.com.br/catalogsearch/result/?q={term}",
        "friolar": "https://www.friolarpecas.com.br/buscar?q={term}",
        "refrigeracao_mota": "https://www.refrigeracaomota.com.br/?s={term}&post_type=product",
        "mg_parts": "https://www.mgparts.com.br/search?type=product&q={term}",
        "gold_service": "https://www.goldservice.com.br/buscar?q={term}",
        "comclick": "https://www.comclick.com.br/loja/busca.php?loja=572273&palavra_busca={term}",
    }
    if store in search_templates:
        return search_templates[store].format(term=quote_plus(term))
    raise ValueError(f"Loja sem busca HTML paginada: {store}")


def amazon_offer_url(asin):
    return f"https://www.amazon.com.br/gp/offer-listing/{asin}"


def _same_store_url(base, href, host):
    absolute = urljoin(base, href or "")
    parsed = urlparse(absolute)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (
        hostname == host or hostname.endswith(f".{host}")
    ):
        return None
    return absolute.split("#", 1)[0]


def _next_page(soup, base, host):
    selectors = (
        'a[rel="next"][href]',
        'a.s-pagination-next[href]',
        '.andes-pagination__button--next a[href]',
        'a[aria-label*="Próxima" i][href]',
        'a[aria-label*="Seguinte" i][href]',
    )
    for selector in selectors:
        link = soup.select_one(selector)
        if link and (next_url := _same_store_url(base, link.get("href"), host)):
            return next_url if next_url != base else None
    for link in soup.select("a[href]"):
        label = " ".join((link.get_text(" ", strip=True), link.get("title") or ""))
        if re.search(r"\b(pr[oó]xima|seguinte|next)\b", label, re.I):
            next_url = _same_store_url(base, link.get("href"), host)
            if next_url and next_url != base:
                return next_url
    return None


def _checked_html(html, final_url):
    if not html or re.search(r"account-verification|captcha|robot-check", final_url, re.I):
        return None, "BLOCKED", "Pagina de verificacao"
    soup = BeautifulSoup(html, "html.parser")
    refreshes = soup.find_all("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if not soup.body or any(not meta.find_parent('noscript') for meta in refreshes):
        return None, "PARSER_ERROR", "HTML sem conteudo de busca"
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if re.search(r"captcha|robot check|access denied|acesso negado|n[aã]o [eé] poss[ií]vel acessar", title, re.I):
        return None, "BLOCKED", title[:200]
    return (soup, final_url), "OK", None


def _fetch_html_result(url, timeout, use_browser=False):
    requested_host = (urlparse(url).hostname or "").lower()
    if requested_host == "mercadolivre.com.br" or requested_host.endswith(".mercadolivre.com.br"):
        from services.mercado_livre_browser import fetch_public_html
        fetched = fetch_public_html(url, timeout, use_browser)
        if fetched["status"] != "OK":
            return None, fetched["status"], fetched.get("reason")
        return _checked_html(fetched["html"], fetched["url"])
    if use_browser:
        rendered = render_discovery_page(url, timeout)
        if not rendered:
            return None, "BROWSER_ERROR", "Navegador nao abriu a pagina"
        requested_host = (urlparse(url).hostname or "").lower()
        final_host = (urlparse(rendered[1]).hostname or "").lower()
        if not final_host or not (
            final_host == requested_host
            or final_host.endswith(f".{requested_host}")
            or requested_host.endswith(f".{final_host}")
        ):
            return None, "BLOCKED", "Redirecionado para outro dominio"
        return _checked_html(*rendered)
    session = create_resilient_session(pool_size=2)
    try:
        with _request_slot(requested_host):
            response = session.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
                    "Accept-Language": "pt-BR,pt;q=0.9",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=(5, timeout),
            )
            response = recover_public_html(response, url, timeout)
        if response.status_code != 200:
            status = "BLOCKED" if response.status_code in {401, 403, 429} else "HTTP_ERROR"
            return None, status, f"HTTP {response.status_code}"
        return _checked_html(response.text, response.url)
    except Exception as exc:
        status = "TIMEOUT" if "timeout" in str(exc).lower() else "HTTP_ERROR"
        return None, status, str(exc)[:200]
    finally:
        session.close()


def _fetch_html(url, timeout):
    return _fetch_html_result(url, timeout)[0]


def fetch_search_page(store, url, timeout=15, diagnostics=None, use_browser=False):
    if store in {'electrolux', 'midea', 'consul', 'brastemp'} and not use_browser:
        from services.manufacturer_pages import sitemap_candidates
        query = parse_qs(urlparse(url).query)
        term = (query.get('q') or query.get('_q') or [''])[0]
        try:
            sitemap_items = sitemap_candidates(store, term, timeout) if term else []
            if sitemap_items:
                if diagnostics is not None:
                    diagnostics.update(status='OK', reason='URLs do sitemap publico; produto sujeito a confirmacao na PDP')
                return sitemap_items, None, True
        except Exception:
            pass  # Keep HTML/browser/indexed discovery available on sitemap failure.
    if diagnostics is None and not use_browser:
        fetched = _fetch_html(url, timeout)
    else:
        fetched, status, reason = _fetch_html_result(url, timeout, use_browser=use_browser)
        if diagnostics is not None:
            diagnostics.update(status=status, reason=reason)
    if not fetched:
        return [], None, False
    soup, final_url = fetched
    host = STORE_HOSTS[store]
    if store == "magalu":
        from services.magalu_public import is_public_storefront
        if is_public_storefront(final_url):
            host = "magazinevoce.com.br"
    items = []
    seen = set()

    if store == "amazon":
        for card in soup.select('div[data-component-type="s-search-result"]'):
            title = card.select_one("h2")
            link = card.select_one("h2 a[href], a.s-no-outline[href]")
            product_url = _same_store_url(final_url, link.get("href"), host) if link else None
            asin = str(card.get("data-asin") or "").upper()
            if re.fullmatch(r"[A-Z0-9]{10}", asin):
                product_url = f"https://www.amazon.com.br/dp/{asin}"
            if title and product_url and product_url not in seen:
                seen.add(product_url)
                items.append({"name": title.get_text(" ", strip=True), "url": product_url, "seller": "Amazon Brasil"})
    elif store == "mercado_livre":
        from crawlers.mercado_livre_public import parse_search_html
        items, ml_next = parse_search_html(str(soup), final_url)
    elif store == "magalu":
        for link in soup.select('a[href*="/p/"]'):
            product_url = _same_store_url(final_url, link.get("href"), host)
            title = link.get_text(" ", strip=True)
            if product_url and title and product_url not in seen:
                seen.add(product_url)
                seller_id = (parse_qs(urlparse(product_url).query).get("seller_id") or [None])[0]
                title_node = link.select_one('[data-testid="product-title"], h2, h3')
                image = link.select_one("img[alt]")
                title = title_node.get_text(" ", strip=True) if title_node else (image.get("alt") if image else title)
                price_node = link.select_one('[data-testid="price-value"]')
                price = number(price_node.get_text(" ", strip=True)) if price_node else None
                items.append({"name": title, "url": product_url, "seller": seller_id + "-Magalu" if seller_id else "Magazine Luiza",
                              **({"price": price} if price is not None and price > 0 else {})})
    else:
        query = parse_qs(urlparse(url).query)
        search_term = next(
            (value[0] for key in ("q", "s", "keyword", "query", "palavra_busca")
             if (value := query.get(key))),
            "",
        )
        identifier = re.sub(r"[^a-z0-9]", "", search_term.lower())
        is_identifier_search = bool(re.fullmatch(r"[a-z]*\d{5,}", identifier))
        selectors = {
            "electrolux": "a[href*='/p']", "midea": "a[href*='/p']",
            "consul": "a[href*='/p']", "brastemp": "a[href*='/p']",
            "leroy_merlin": ".product-card a[href], a[href*='/produto/']",
            "shopee": "a[href*='-i.'], a[href*='/product/']",
            "dufrio": "li.product a[href], .product-item a[href]",
            "friolar": ".listagem-item .nome-produto[href]",
            "refrigeracao_mota": "li.product a.product-loop-title[href]",
            "mg_parts": "product-card .product-card-title[href], .product-card a[href*='/products/']",
            "gold_service": ".listagem-item .nome-produto[href]",
            "comclick": ".product a.product-info[href], .product a.product-button[href]",
        }
        listing_soup = _decoded_template_soup(soup) if store == "refrigeracao_mota" else soup
        for link in listing_soup.select(selectors[store]):
            product_url = _same_store_url(final_url, link.get("href"), host)
            if store in {'electrolux', 'midea', 'consul', 'brastemp'} and (not product_url or not urlparse(product_url).path.rstrip('/').endswith('/p')):
                continue
            title = link.get_text(" ", strip=True) or link.get("title") or ""
            if store == "refrigeracao_mota":
                card = link.find_parent("li", class_="product")
                name_node = card.select_one(".woocommerce-loop-product__title") if card else None
                title = name_node.get_text(" ", strip=True) if name_node else title
            if is_identifier_search and identifier not in re.sub(
                r"[^a-z0-9]", "", f"{title} {product_url}".lower()
            ):
                if store == 'mg_parts' and product_url and title and not re.search(r'\bW\d{8}\b', title, re.I):
                    # Shopify can search variant SKUs that are absent from the
                    # card title/URL. These URLs must be verified in the PDP.
                    items.append({"name": title, "url": product_url, "seller": SEARCH_LABELS[store],
                                  "_requires_pdp_verification": True})
                continue
            if product_url and title and product_url not in seen:
                seen.add(product_url)
                items.append({"name": title, "url": product_url, "seller": SEARCH_LABELS[store]})
        if store == "dufrio" and final_url != url:
            title_node = soup.select_one("h1")
            title = title_node.get_text(" ", strip=True) if title_node else ""
            if title and final_url not in seen:
                items.append({"name": title, "url": final_url, "seller": SEARCH_LABELS[store]})

    if store == 'mg_parts' and any(not item.get('_requires_pdp_verification') for item in items):
        items = [item for item in items if not item.get('_requires_pdp_verification')]
    next_url = _next_page(soup, final_url, host)
    if store == "mercado_livre":
        next_url = ml_next or next_url
    if not items and not next_url:
        no_results = re.search(
            r"nenhum(?:a)? (?:produto|resultado|an[uú]ncio)|sem resultados|no results",
            soup.get_text(" ", strip=True), re.I,
        )
        if not no_results:
            if diagnostics is not None:
                diagnostics.update(status="PARSER_ERROR", reason="Busca sem cards nem mensagem de resultado vazio")
            return [], None, False
    return items, next_url, True


def fetch_amazon_offer_page(asin, url, identity, timeout=15, diagnostics=None, use_browser=False):
    if diagnostics is None and not use_browser:
        fetched = _fetch_html(url, timeout)
    else:
        fetched, status, reason = _fetch_html_result(url, timeout, use_browser=use_browser)
        if diagnostics is not None:
            diagnostics.update(status=status, reason=reason)
    if not fetched:
        return [], None, False
    soup, final_url = fetched
    items = []
    seen = set()
    for card in soup.select(".olpOffer, .aod-offer, [id^='aod-offer']"):
        seller_node = card.select_one(".olpSellerName, #aod-offer-soldBy")
        seller = seller_node.get_text(" ", strip=True) if seller_node else ""
        seller_link = card.select_one(".olpSellerName a[href], #aod-offer-soldBy a[href]")
        if seller_link:
            seller = seller_link.get_text(" ", strip=True)
        seller = re.sub(r"^(?:vendido\s+por|sold\s+by)\s*:?\s*", "", seller, flags=re.I)
        seller_id = None
        if seller_link:
            seller_id = (parse_qs(urlparse(seller_link.get("href")).query).get("seller") or [None])[0]
        if not seller:
            continue
        key = seller_id or seller
        if key in seen:
            continue
        seen.add(key)
        query = f"?{urlencode({'m': seller_id})}" if seller_id else ""
        items.append({
            "name": identity.name or identity.sku,
            "model": identity.model or identity.sku,
            "asin": asin,
            "seller": seller,
            "store": seller,
            "source": "Amazon Brasil",
            "url": f"https://www.amazon.com.br/dp/{asin}{query}",
            **({"price": price} if (price := number(
                (price_node := card.select_one(".a-price .a-offscreen, .olpOfferPrice"))
                and price_node.get_text(" ", strip=True)
            )) is not None else {}),
        })
    next_url = _next_page(soup, final_url, STORE_HOSTS["amazon"])
    if not items and not next_url and diagnostics is not None:
        diagnostics.update(status="PARSER_ERROR", reason="Pagina de vendedores sem ofertas legiveis")
    return items, next_url, bool(items or next_url)


def _jsonld_products(soup):
    def walk(value):
        if isinstance(value, list):
            for child in value:
                yield from walk(child)
        elif isinstance(value, dict):
            types = value.get("@type") or []
            types = types if isinstance(types, list) else [types]
            if any(str(kind).rsplit("/", 1)[-1] == "Product" for kind in types):
                yield value
            yield from walk(value.get("@graph"))

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (TypeError, ValueError):
            continue


def _individual_offers(value):
    if isinstance(value, list):
        for child in value:
            yield from _individual_offers(child)
    elif isinstance(value, dict):
        types = value.get("@type") or []
        types = types if isinstance(types, list) else [types]
        if "AggregateOffer" in {str(kind).rsplit("/", 1)[-1] for kind in types}:
            yield from _individual_offers(value.get("offers"))
        else:
            yield value


def _listing_price(value):
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    cleaned = re.sub(r"[^\d,.]", "", str(value or ""))
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        price = float(cleaned)
    except ValueError:
        return None
    return price if price > 0 else None


def fetch_seller_offer_page(store, url, identity, timeout=15, diagnostics=None, use_browser=False):
    """Read public individual offers and follow seller pagination for one PDP."""
    if diagnostics is None and not use_browser:
        fetched = _fetch_html(url, timeout)
    else:
        fetched, status, reason = _fetch_html_result(url, timeout, use_browser=use_browser)
        if diagnostics is not None:
            diagnostics.update(status=status, reason=reason)
    if not fetched:
        return [], None, False
    soup, final_url = fetched
    host = STORE_HOSTS[store]
    if store == 'magalu':
        from services.magalu_public import embedded_seller_offers, is_public_storefront
        from scrapy.http import HtmlResponse
        hydrated = embedded_seller_offers(HtmlResponse(url=final_url, body=str(soup).encode(), encoding='utf-8'), identity)
        if is_public_storefront(final_url):
            host = 'magazinevoce.com.br'
        if hydrated:
            return [dict(name=row['name'], url=row['url'], seller=party_name(row['seller']),
                         price=row['price'], cash_price=row.get('cash_price'),
                         installment_price=row.get('installment_price'),
                         availability=row.get('availability'), discovery_method='seller_pages')
                    for row in hydrated], _next_page(soup, final_url, host), True
    if store == 'mercado_livre':
        from types import SimpleNamespace
        from crawlers.mercado_livre_public import parse_product_html
        source = SimpleNamespace(store=store, url=final_url, seller=None, brand=None,
                                 external_product_id=None, asin=None, item_id=None)
        parsed = parse_product_html(str(soup), final_url, source, identity)
        if parsed['status'] != 'OK':
            if diagnostics is not None:
                diagnostics.update(status=parsed['status'], reason=parsed.get('reason'))
            return [], None, False
        return parsed['items'], _next_page(soup, final_url, host), True
    items = []
    seen = set()
    title_node = soup.select_one("h1, meta[property='og:title']")
    page_title = (
        title_node.get("content") if title_node and title_node.name == "meta"
        else title_node.get_text(" ", strip=True) if title_node else ""
    )
    nodes = product_nodes(str(soup), final_url)
    for product in (node for node in nodes if "Product" in types(node)):
        if not resolve_candidate(identity, product_candidate(product)).accepted:
            continue
        title = product.get("name") or page_title
        if not title:
            continue
        for offer in product_offers(product, nodes):
            seller = party_name(offer.get("seller"))
            seller = str(seller or "").strip()
            offer_url = _same_store_url(final_url, offer.get("url") or final_url, host)
            if not seller or not offer_url or (seller, offer_url) in seen:
                continue
            seen.add((seller, offer_url))
            price = offer_price(offer)
            items.append({
                "name": title, "url": offer_url, "seller": seller,
                "price": price, "discovery_method": "seller_pages",
            })
    next_url = _next_page(soup, final_url, host)
    if not next_url:
        for link in soup.select("a[href]"):
            if re.search(r"outr[oa]s? (?:ofertas|vendedores)|mais ofertas|ver ofertas", link.get_text(" ", strip=True), re.I):
                candidate = _same_store_url(final_url, link.get("href"), host)
                if candidate and candidate != final_url:
                    next_url = candidate
                    break
    # A heading alone does not establish that seller enumeration succeeded.
    # Error pages and PDPs that hide sellers behind JS must reach the fallback.
    if diagnostics is not None and not (items or next_url):
        diagnostics.update(status="PARSER_ERROR", reason="Pagina sem ofertas individuais de vendedores legiveis")
    return items, next_url, bool(items or next_url)

from __future__ import annotations
from services.shipping_selection import shipping_sort_key

import re
import unicodedata
from datetime import datetime
from threading import BoundedSemaphore
from urllib.parse import urlparse


BROWSER_SLOT = BoundedSemaphore(2)


def _skip_heavy_assets(page):
    page.route(
        "**/*",
        lambda route: route.abort() if route.request.resource_type in {"image", "font", "media"}
        else route.continue_(),
    )


def render_discovery_page(url, timeout_seconds=15):
    """Render one public discovery page; parsing and host checks stay with the caller."""
    with BROWSER_SLOT:
        try:
            from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright

            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, timeout=10000)
                try:
                    page = browser.new_page(locale="pt-BR")
                    page.set_default_timeout(timeout_seconds * 1000)
                    _skip_heavy_assets(page)
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                    if response and response.status >= 400:
                        return page.content(), page.url
                    if "/gp/offer-listing/" in url:
                        choices = page.locator('#aod-ingress-link, #buybox-see-all-buying-choices, [data-action="show-all-offers-display"]')
                        if choices.count():
                            try:
                                choices.first.click(timeout=2000)
                            except BrowserTimeout:
                                pass
                    try:
                        page.wait_for_function(r"""() => {
                            const selectors = 'div[data-component-type="s-search-result"], .poly-card, .ui-search-layout__item, a[href*="/p/"], .aod-offer, .olpOffer, .listagem-item, .product-card, li.product, .result, li.b_algo';
                            return !!document.querySelector(selectors)
                                || Array.from(document.querySelectorAll('script[type="application/ld+json"]')).some(script => /"offers"\s*:/.test(script.textContent) && /"Product"/.test(script.textContent))
                                || /nenhum(?:a)? (produto|resultado)|sem resultados|no results|captcha|access denied|acesso negado/i.test(document.body?.innerText || '');
                        }""", timeout=min(timeout_seconds * 1000, 5000))
                    except BrowserTimeout:
                        pass
                    return page.content(), page.url
                finally:
                    browser.close()
        except Exception as exc:
            print(f"[FALLBACK][BROWSER_DISCOVERY] {exc}")
            return None


def render_product_page(url, timeout_seconds=15, page_handler=None, ready_identifier=None):
    """Render a public PDP with bounded navigation, JS wait and guaranteed cleanup."""
    with BROWSER_SLOT:
        try:
            from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright

            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, timeout=10000)
                try:
                    page = browser.new_page(locale="pt-BR")
                    page.set_default_timeout(timeout_seconds * 1000)
                    _skip_heavy_assets(page)
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                    status = response.status if response else 0
                    if status in {401, 403, 429}:
                        return {"status": "BLOCKED", "reason": f"Navegador HTTP {status}"}
                    if status in {404, 410}:
                        return {"status": "NOT_FOUND", "reason": f"Navegador HTTP {status}"}
                    if status != 200:
                        return {"status": "PARSER_ERROR", "reason": f"Navegador HTTP {status}"}
                    if urlparse(page.url).hostname != urlparse(url).hostname:
                        return {"status": "INVALID_MATCH", "reason": "PDP redirecionou para outro dominio"}
                    if re.search(r"captcha|robot check|access denied|acesso negado", page.title(), re.I):
                        return {"status": "BLOCKED", "reason": "Pagina de verificacao no navegador"}
                    try:
                        page.wait_for_function(
                            """identifier => [...document.querySelectorAll('script[type="application/ld+json"]')]
                            .some(n => /Product/.test(n.textContent) && (!identifier || n.textContent.toLowerCase().includes(identifier.toLowerCase())))
                            || !!document.querySelector('.product-info-main [itemprop="price"], #corePrice_feature_div .a-offscreen, [data-testid="product-price"]')""",
                            arg=ready_identifier,
                            timeout=min(4000, timeout_seconds * 1000),
                        )
                    except BrowserTimeout:
                        pass
                    return page_handler(page) if page_handler else {"status": "OK", "html": page.content(), "url": page.url}
                finally:
                    browser.close()
        except Exception as exc:
            return {"status": "BROWSER_ERROR", "reason": str(exc)[:300]}


def collect_product_with_playwright(source, identity, postal_code=None, timeout_seconds=15, include_shipping=False):
    if source.store == "mercado_livre":
        from crawlers.mercado_livre_public import collect_public_product
        return collect_public_product(source, identity, postal_code, timeout_seconds, use_browser=True)
    from scrapy.http import HtmlResponse
    from crawlers.base import parse_product_response
    from crawlers.profiles import DEFAULT_PROFILE, STORE_PROFILES

    def parse_page(page):
        from services.structured_products import merge_offer_details, offer_available
        response = HtmlResponse(url=page.url, body=page.content().encode("utf-8"), encoding="utf-8")
        parsed = parse_product_response(response, source, identity, STORE_PROFILES.get(source.store, DEFAULT_PROFILE), postal_code)
        items = parsed.get("items", [])
        if include_shipping and postal_code and len(items) == 1 and offer_available(items[0]):
            try:
                quote = _browser_platform_quote(page, postal_code, timeout_seconds)
            except Exception as exc:
                print(f"[FALLBACK][LOGISTICS] {str(exc)[:200]}")
                quote = {}
            if quote:
                items[0] = merge_offer_details(items[0], quote)
                items[0]["_logistics_attempted_zip"] = postal_code
                parsed["item"] = items[0]
        return parsed

    rendered = render_product_page(source.url, timeout_seconds, page_handler=parse_page,
                                   ready_identifier=identity.ean or identity.mpn or identity.model or identity.sku)
    if rendered["status"] != "OK":
        return rendered
    if "items" in rendered:
        parsed = rendered
    else:
        response = HtmlResponse(url=rendered["url"], body=rendered["html"].encode("utf-8"), encoding="utf-8")
        parsed = parse_product_response(response, source, identity, STORE_PROFILES.get(source.store, DEFAULT_PROFILE), postal_code)
    for item in parsed.get("items", []):
        item["discovery_source"] = "browser_pdp"
    return parsed


def _number(value):
    from services.structured_products import number
    return number(value, allow_zero=True)


def _shipping_texts(page):
    texts = page.evaluate("""() => {
      const selectors = '[id*="frete" i], [class*="frete" i], [id*="shipping" i], [class*="shipping" i], [data-testid*="delivery" i], [data-testid*="shipping" i]';
      const roots = [...document.querySelectorAll(selectors)].filter(node =>
        node.getClientRects().length && !node.closest('.related, .upsells, .recommendations, [id*="recommend" i], [class*="recommend" i]'));
      return [...new Set(roots.flatMap(node => {
        const rows = [...node.querySelectorAll('tr, .shipping-option, .shipping-method, li')];
        return (rows.length ? rows : [node]).map(row => row.innerText || '');
      }))].filter(text => /R\\$|frete\\s+gr[aá]tis|\\d+\\s*dias?/i.test(text));
    }""")
    return [str(text) for text in texts] if isinstance(texts, list) else []


def _parse_shipping_quote(text):
    from services.product_resolver import normalize_text
    normalized = normalize_text(text)
    if re.search(r"\b(?:retir[a-z]*|pickup|coleta)\b", normalized):
        return {}
    free = bool(re.search(r"\bfrete gratis\b", normalized))
    if free and re.search(r"acima|a partir|compras|prime|assinantes|membros", normalized):
        return {}
    costs = [_number(match.group(1)) for match in re.finditer(r"R\$\s*([\d.,]+)", text)]
    costs = [cost for cost in costs if cost is not None]
    shipping = min((cost for cost in costs if cost > 0), default=0.0 if free else None)
    days_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    days = re.search(r"\b(\d{1,3})(?:\s*(?:a|ate|-)\s*(\d{1,3}))?\s+dias?\b", days_text)
    if shipping is None and not days:
        return {}
    result = {"shipping_cost": shipping}
    if days and len(costs) <= 1:
        minimum, maximum = int(days.group(1)), int(days.group(2) or days.group(1))
        if maximum >= minimum:
            result.update(delivery_min_days=minimum, delivery_max_days=maximum)
            result["delivery_days_type"] = "business" if "uteis" in normalized else "calendar"
    return {key: value for key, value in result.items() if value is not None}


def _browser_platform_quote(page, postal_code, timeout_seconds):
    """Use public, anonymous delivery calculators from the same product page."""
    from services.offer_enrichment import _loja_integrada_rate_details
    result = page.evaluate("""async ({postalCode, timeout}) => {
      const product = window.PRODUTO_ID;
      const quoteUrl = window.URL_PRODUTO_FRETE_CALCULAR;
      const request = (url, options={}) => fetch(url, {...options, credentials:'same-origin', signal:AbortSignal.timeout(timeout)});
      try {
        if (product && quoteUrl) {
          const endpoint = new URL(quoteUrl, location.href);
          if (endpoint.origin !== location.origin) return null;
          endpoint.search = new URLSearchParams({cep:postalCode,produto_id:String(product),quantidade:'1'});
          const response = await request(endpoint,{headers:{Accept:'application/json','X-Requested-With':'XMLHttpRequest'}});
          return response.ok ? {provider:'loja_integrada',rates:await response.json()} : null;
        }
        if (!document.body.classList.contains('woocommerce')) return null;
        const button = document.querySelector('form.cart [name="add-to-cart"], form.cart [name="product_id"]');
        const variation = document.querySelector('form.cart [name="variation_id"]');
        if (!button || variation && !Number(variation.value)) return null;
        const productId = Number(variation?.value || button.value);
        if (!productId) return null;
        const base = '/wp-json/wc/store/v1/cart';
        const cart = await request(base);
        if (!cart.ok) return null;
        const nonce = cart.headers.get('Nonce');
        const token = cart.headers.get('Cart-Token');
        if (!nonce && !token) return null;
        const headers = {'Content-Type':'application/json',Accept:'application/json'};
        if (nonce) headers.Nonce = nonce;
        if (token) headers['Cart-Token'] = token;
        const added = await request(base+'/add-item',{method:'POST',headers,body:JSON.stringify({id:productId,quantity:1})});
        if (!added.ok) return null;
        let state = '';
        try {
          const address = await fetch('https://viacep.com.br/ws/'+postalCode+'/json/',{signal:AbortSignal.timeout(timeout)});
          if (address.ok) state = (await address.json()).uf || '';
        } catch {}
        const address = {country:'BR',postcode:postalCode};
        if (state) address.state = state;
        const updated = await request(base+'/update-customer',{method:'POST',headers,body:JSON.stringify({shipping_address:address})});
        return updated.ok ? {provider:'woocommerce',cart:await updated.json()} : null;
      } catch { return null; }
    }""", {"postalCode": postal_code, "timeout": min(12000, timeout_seconds * 1000)})
    if not isinstance(result, dict):
        return {}
    provider = result.get("provider") or "loja_integrada"
    details = _woocommerce_rate_details(result.get("cart")) if provider == "woocommerce" else _loja_integrada_rate_details(result.get("rates"))
    if any(details.get(field) is not None for field in ("shipping_cost", "delivery_max_days")):
        return {**details, "logistics_source": f"{provider}_browser", "destination_zip": postal_code,
                "logistics_captured_at": datetime.utcnow().isoformat()}
    return {}


def _woocommerce_rate_details(cart):
    from services.structured_products import delivery_range, number
    if not isinstance(cart, dict):
        return {}
    selected = []
    for package in cart.get("shipping_rates") or []:
        options = []
        for rate in package.get("shipping_rates") or []:
            name = str(rate.get("name") or "")
            if re.search(r"retir|pickup|coleta", name + str(rate.get("rate_id") or ""), re.I):
                continue
            if str(rate.get("currency_code") or "BRL").upper() != "BRL":
                continue
            cost = number(rate.get("price"), allow_zero=True)
            minor = rate.get("currency_minor_unit", 2)
            if cost is None or not isinstance(minor, int) or not 0 <= minor <= 4:
                continue
            metadata = {item.get("key"): item.get("value") for item in rate.get("meta_data", []) if isinstance(item, dict)}
            days = next((metadata[key] for key in ("delivery_time", "deliveryTime", "prazo") if metadata.get(key) is not None), name)
            minimum, maximum = delivery_range(days)
            options.append({"shipping_cost": round(cost / 10 ** minor, 2), "shipping_method": name,
                            "delivery_min_days": minimum, "delivery_max_days": maximum,
                            "delivery_days_type": "business" if re.search(r"[uú]teis", str(days), re.I) else None})
        if not options:
            return {}
        selected.append(min(options, key=lambda option: shipping_sort_key(option["shipping_cost"], option.get("delivery_max_days"))))
    if not selected:
        return {}
    return {"shipping_cost": round(sum(option["shipping_cost"] for option in selected), 2),
            "shipping_method": " / ".join(option["shipping_method"] for option in selected),
            "delivery_min_days": max(option["delivery_min_days"] for option in selected) if all(option["delivery_min_days"] is not None for option in selected) else None,
            "delivery_max_days": max(option["delivery_max_days"] for option in selected) if all(option["delivery_max_days"] is not None for option in selected) else None,
            "delivery_days_type": selected[0]["delivery_days_type"] if all(option["delivery_days_type"] == selected[0]["delivery_days_type"] for option in selected) else None}


def collect_shipping_with_playwright(url, postal_code, timeout_seconds=15):
    if not postal_code:
        return {}
    from services.browser_health import shipping_form_available, record_missing_shipping_form, record_shipping_form_found
    if not shipping_form_available(url):
        return {"_unsupported_shipping": True}
    with BROWSER_SLOT:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True, timeout=10000)
                try:
                    page = browser.new_page(locale="pt-BR")
                    page.set_default_timeout(timeout_seconds * 1000)
                    _skip_heavy_assets(page)
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                    if response and response.status in {403, 429}:
                        return {"_blocked_status": response.status}
                    if urlparse(page.url).hostname != urlparse(url).hostname:
                        return {}
                    platform_quote = _browser_platform_quote(page, postal_code, timeout_seconds)
                    if platform_quote:
                        record_shipping_form_found(url)
                        return platform_quote
                    zip_input = page.locator(
                        'input[placeholder*="CEP" i], input[name*="cep" i], input[id*="cep" i], '
                        'input[placeholder*="postal" i]'
                    ).first
                    if not zip_input.count():
                        try:
                            zip_input.wait_for(state='attached', timeout=min(3000, timeout_seconds * 1000))
                        except Exception:
                            record_missing_shipping_form(url)
                            return {"_unsupported_shipping": True}
                    record_shipping_form_found(url)
                    before = _shipping_texts(page)
                    zip_input.fill(postal_code)
                    form = zip_input.locator("xpath=ancestor::form[1]")
                    button = form.locator('button:has-text("Calcular"), button:has-text("Consultar"), button[type="submit"], input[type="submit"]').first
                    if button.count():
                        button.click()
                    else:
                        zip_input.press("Enter")
                    page.wait_for_function("""previous => [...document.querySelectorAll(
                        '[id*="frete" i], [class*="frete" i], [id*="shipping" i], [class*="shipping" i], [data-testid*="delivery" i]'
                    )].some(node => node.getClientRects().length &&
                        /R\\$|frete\\s+gr[aá]tis|\\d+\\s*dias?/i.test(node.innerText || '') &&
                        !previous.includes(node.innerText || ''))""",
                        arg=before, timeout=min(7000, timeout_seconds * 1000),
                    )
                    after = _shipping_texts(page)
                finally:
                    browser.close()
        except Exception as exc:
            print(f"[FALLBACK][PLAYWRIGHT] PARSER_ERROR: {exc}")
            return {}

    if before == after:
        return {}
    old_lines = {line.strip() for text in before for line in text.splitlines()}
    options = [_parse_shipping_quote("\n".join(
        line for line in text.splitlines() if line.strip() not in old_lines
    )) for text in after]
    options = [option for option in options if option]
    if not options:
        return {}
    selected = min(options, key=lambda option: shipping_sort_key(option.get("shipping_cost"), option.get("delivery_max_days")))
    result = {
        **selected,
        "logistics_source": "playwright_pdp",
        "destination_zip": postal_code,
        "logistics_captured_at": datetime.utcnow().isoformat(),
    }
    result = {key: value for key, value in result.items() if value is not None}
    print("[FALLBACK][PLAYWRIGHT] frete/prazo coletado na PDP")
    return result

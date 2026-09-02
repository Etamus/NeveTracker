from __future__ import annotations

import re
from threading import BoundedSemaphore


BROWSER_SLOT = BoundedSemaphore(1)


def _number(value):
    cleaned = re.sub(r"[^\d,.]", "", str(value or ""))
    if not cleaned:
        return None
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def collect_shipping_with_playwright(url, postal_code, timeout_seconds=15):
    if not postal_code:
        return {}
    with BROWSER_SLOT:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(headless=True)
                page = browser.new_page(locale="pt-BR")
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
                zip_input = page.locator(
                    'input[placeholder*="CEP" i], input[name*="cep" i], input[id*="cep" i], '
                    'input[placeholder*="postal" i]'
                ).first
                if zip_input.count():
                    zip_input.fill(postal_code)
                    form = zip_input.locator("xpath=ancestor::form[1]")
                    button = form.locator('button[type="submit"], input[type="submit"]').first
                    if button.count():
                        button.click()
                    else:
                        zip_input.press("Enter")
                    page.wait_for_timeout(1800)
                text = page.locator("body").inner_text(timeout=5000)
                browser.close()
        except Exception as exc:
            print(f"[FALLBACK][PLAYWRIGHT] PARSER_ERROR: {exc}")
            return {}

    shipping = None
    if re.search(r"frete\s+gr[aá]tis", text, re.I):
        shipping = 0.0
    else:
        match = re.search(r"(?:frete|entrega)[^R$]{0,40}R\$\s*[\d.,]+", text, re.I)
        shipping = _number(match.group()) if match else None
    days = re.search(
        r"(?:entrega|receba|prazo)[^\d]{0,40}(\d{1,3})(?:\s*(?:a|at[eé]|-)\s*(\d{1,3}))?\s+dias?",
        text,
        re.I,
    )
    result = {
        "shipping_cost": shipping,
        "delivery_min_days": int(days.group(1)) if days else None,
        "delivery_max_days": int(days.group(2) or days.group(1)) if days else None,
        "logistics_source": "playwright_pdp",
        "destination_zip": postal_code,
    }
    result = {key: value for key, value in result.items() if value is not None}
    if result:
        print("[FALLBACK][PLAYWRIGHT] frete/prazo coletado na PDP")
    return result

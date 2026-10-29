"""Classify delivery responsibility only from an explicit shipping statement.

Interface convention requested by the user: 2P = platform delivery,
3P = merchant delivery. Unknown responsibility is deliberately left blank.
"""
import re
from bs4 import BeautifulSoup
from services.product_resolver import normalize_text


def fulfillment_details(html, source, seller):
    soup = BeautifulSoup(html, 'html.parser')
    statements = []
    for node in soup.select('#tabular-buybox, #shipsFromSoldBy_feature_div, #merchant-info, [data-testid*="delivery"], [data-testid*="shipping"], .ui-pdp-shipping, [itemprop="shippingDetails"]'):
        if not node.find_parent(class_=re.compile('recommend|related|upsell', re.I)):
            statements.append(node.get_text(' ', strip=True))
    for text in statements:
        if normalize_text(source) == 'mercado livre' and re.search(r'\b(?:envio|entrega)\s+full\b', normalize_text(text)):
            return {'fulfillment_type': '2P', 'fulfilled_by': 'Mercado Livre', 'fulfillment_evidence': text[:200]}
        match = re.search(r'(?:enviado|entregue|despachado)\s+(?:e\s+entregue\s+)?por\s*:?\s*(.{2,100}?)(?=\s+(?:vendido por|devolu|saiba mais|prazo|frete)|[.;|]|$)', text, re.I)
        if not match:
            continue
        party = match.group(1).strip()
        compact = normalize_text(party)
        platform = normalize_text(source)
        merchant = normalize_text(seller)
        platform_aliases = {'amazon brasil': {'amazon', 'amazon com br'}, 'magazine luiza': {'magalu', 'magazine luiza'}, 'mercado livre': {'mercado livre', 'mercadolivre'}}
        if compact in {platform, *platform_aliases.get(platform, set())}:
            return {'fulfillment_type': '2P', 'fulfilled_by': party, 'fulfillment_evidence': match.group(0)}
        if compact == merchant and merchant:
            return {'fulfillment_type': '3P', 'fulfilled_by': party, 'fulfillment_evidence': match.group(0)}
        return {'fulfilled_by': party, 'fulfillment_evidence': match.group(0)}
    return {}

from crawlers import amazon, comclick, dufrio, friolar, goldservice, leroy, magalu, mgparts, mota
from crawlers.mercado_livre import collect_mercado_livre
from crawlers.shopee import collect_shopee


def collect_manufacturer(source, identity, postal_code=None, timeout=15):
    from crawlers.base import collect_http
    from crawlers.profiles import DEFAULT_PROFILE
    return collect_http(source, identity, postal_code, DEFAULT_PROFILE, timeout)


HTTP_COLLECTORS = {
    "electrolux": collect_manufacturer, "midea": collect_manufacturer,
    "consul": collect_manufacturer, "brastemp": collect_manufacturer,
    "amazon": amazon.collect,
    "magalu": magalu.collect,
    "leroy_merlin": leroy.collect,
    "dufrio": dufrio.collect,
    "friolar": friolar.collect,
    "refrigeracao_mota": mota.collect,
    "mg_parts": mgparts.collect,
    "gold_service": goldservice.collect,
    "comclick": comclick.collect,
}


def collect_product_source(source, identity, postal_code=None, timeout=15):
    if source.store == "mercado_livre":
        return collect_mercado_livre(source, identity, postal_code, timeout)
    if source.store == "shopee":
        return collect_shopee(source, identity, postal_code, timeout)
    collector = HTTP_COLLECTORS.get(source.store)
    if not collector:
        return {"status": "PARSER_ERROR", "reason": "Coletor nao registrado"}
    return collector(source, identity, postal_code, timeout)

from crawlers.base import collect_http
from crawlers.profiles import STORE_PROFILES


def collect(source, identity, postal_code=None, timeout=15):
    return collect_http(source, identity, postal_code, STORE_PROFILES["leroy_merlin"], timeout)

"""Compatibility entry points for the persistent Mercado Livre account session."""
from services.mercado_livre_browser import PROFILE_DIR

VALIDATION_METADATA = PROFILE_DIR.parent / 'mercado-livre-validation.json'


def validated_after(when):
    from services.marketplace_session import validated_after as verified
    return verified('mercado_livre',when)


def session_status():
    from services.marketplace_session import session_status as status
    return status('mercado_livre')


def start_validation():
    from services.marketplace_session import start_validation as start
    return start('mercado_livre')

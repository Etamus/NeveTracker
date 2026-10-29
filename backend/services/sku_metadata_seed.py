from datetime import datetime

from models.product import KnownSku


# Removed from the catalog at the user's request; do not rediscover from compatibility titles.
RETIRED_AUTO_SEED_SKUS = frozenset(['326037942', 'W10728409', 'W10859619', 'W11433177', 'W11525615', 'W11677210'])


SKU_BUSINESS_METADATA = {
    "W11509387": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11199342": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11509386": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W10581058": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "W10624070": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "W11387305": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Cilindros"),
    "W10585027": ("PEÇAS+", "CDP Bronze", "Bombas"),
    "W10894262": ("PEÇAS+", "CDP Bronze", "Manípulos"),
    "W11298039": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "W10510889": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W10200101": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W10907728": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Acessórios"),
    "W10544750": ("PEÇAS+", "CDP Bronze", "Motores"),
    "W10577330": ("PEÇAS+", "CDP Bronze", "Tampas Móveis"),
    "W11393157": ("PEÇAS+", "CDP Bronze", "Tampas Móveis"),
    "W10866791": ("PEÇAS+", "CDP Diamante", "Kits Instalação"),
    "W10347201": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W11601002": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W10705755": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Acessórios"),
    "W10644162": ("PEÇAS+", "CDP Bronze", "Tampas Móveis"),
    "W10420716": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Acessórios"),
    "W10758992": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "W10866790": ("PEÇAS+", "CDP Bronze", "Kits Instalação"),
    "W11108178": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W11661613": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W11228626": ("PEÇAS+", "CDP Bronze", "Placas de Interface"),
    "W11396368": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "326047477": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "326065620": ("PEÇAS+", "CDP Bronze", "Outros"),
    "W10212150": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "W11619403": ("PEÇAS+", "CDP Bronze", "Placas de Controle"),
    "W10911678": ("PEÇAS+", "CDP Bronze", "Trempes"),
    "W10657521": ("PEÇAS+", "CDP Bronze", "Portas"),
    "326043145": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11393065": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "326075868": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11375406": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W10686098": ("PEÇAS+", "CDP Bronze", "Manípulos"),
    "W11047054": ("PEÇAS+", "CDP Bronze", "Interruptores horário"),
    "W10476183": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W10169457": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W11601003": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11619317": ("PEÇAS+", "CDP Bronze", "Placas de Controle"),
    "W10833267": ("PEÇAS+", "CDP Bronze", "Parafusos e afins"),
    "W11670790": ("PEÇAS+", "CDP Bronze", "Emblemas"),
    "W10515645": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11559186": ("PEÇAS+", "CDP Diamante", "Kits Instalação"),
    "W11185123": ("PEÇAS+", "CDP Bronze", "Tampas Móveis"),
    "W10911706": ("PEÇAS+", "CDP Bronze", "Queimadores"),
    "W11200625": ("PEÇAS+", "CDP Bronze", "Placas de Controle"),
    "W10390580": ("PEÇAS+", "CDP Bronze", "Trempes"),
    "W10544749": ("PEÇAS+", "CDP Bronze", "Motores"),
    "W10901034": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11198953": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W10301562": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11100357": ("PEÇAS+", "D2C Marketplace", "Tubos Queima"),
    "W10866789": ("PEÇAS+", "CDP Bronze", "Kits Instalação"),
    "W11360276": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W10287547": ("PEÇAS+", "CDP Bronze", "Gavetas"),
    "W10287527": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W11132264": ("PEÇAS+", "CDP Bronze", "Trempes"),
    "W10320833": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W10351342": ("PEÇAS+", "CDP Bronze", "Ice Maker"),
    "W10638541": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11300695": ("PEÇAS+", "CDP Bronze", "Mecanismos"),
    "W11100356": ("PEÇAS+", "CDP Prata", "Acessórios"),
    "W10915770": ("PEÇAS+", "CDP Bronze", "Vidros"),
    "326075876": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "326073242": ("PEÇAS+", "CDP Bronze", "Outros"),
    "W10455522": ("PEÇAS+", "CDP Bronze", "Prateleiras"),
    "W10638550": ("CONSUMÍVEIS & ACESSÓRIOS", "CDP Bronze", "Filtros"),
    "W11245249": ("BEST SELLER", "CDP Bronze", "Válvulas de Água"),
    "W10806305": ("PEÇAS+", "CDP Bronze", "Placas de Controle"),
    "W10544738": ("PEÇAS+", "CDP Bronze", "Mangueiras"),
}

SKU_CATEGORIES = {
    "W11509387": "PURIFICADORES",
    "W11199342": "PURIFICADORES",
    "W11509386": "PURIFICADORES",
    "W10581058": "REFRIGERADORES",
    "W10624070": "REFRIGERADORES",
    "W11387305": "B-BLEND",
    "W10585027": "LAVA LOUÇAS",
    "W10894262": "FOGÕES",
    "W11298039": "REFRIGERADORES",
    "W10510889": "REFRIGERADORES",
    "W10200101": "REFRIGERADORES",
    "W10907728": "REFRIGERADORES",
    "W10544750": "LAVA LOUÇAS",
    "W10577330": "FOGÕES",
    "W11393157": "LAVADORAS",
    "W10866791": "FOGÕES",
    "W10347201": "REFRIGERADORES",
    "W11601002": "PURIFICADORES",
    "W10705755": "LAVADORAS",
    "W10644162": "FOGÕES",
    "W10420716": "REFRIGERADORES",
    "W10758992": "REFRIGERADORES",
    "W10866790": "FOGÕES",
    "W11108178": "REFRIGERADORES",
    "W11661613": "REFRIGERADORES",
    "W11228626": "REFRIGERADORES",
    "W11396368": "REFRIGERADORES",
    "326047477": "FREEZER",
    "326065620": "FREEZER",
    "W10212150": "REFRIGERADORES",
    "W11619403": "LAVA LOUÇAS",
    "W10911678": "FOGÕES",
    "W10657521": "REFRIGERADORES",
    "326043145": "SECADORAS",
    "W11393065": "REFRIGERADORES",
    "326075868": "COIFAS / DEPURADORES",
    "W11375406": "REFRIGERADORES",
    "W10686098": "FOGÕES",
    "W11047054": "SECADORAS",
    "W10476183": "REFRIGERADORES",
    "W10169457": "REFRIGERADORES",
    "W11601003": "PURIFICADORES",
    "W11619317": "LAVA LOUÇAS",
    "W10833267": "LAVA LOUÇAS",
    "W11670790": "REFRIGERADORES",
    "W10515645": "REFRIGERADORES",
    "W11559186": "FOGÕES",
    "W11185123": "FOGÕES",
    "W10911706": "FOGÕES",
    "W11200625": "COOKTOPS",
    "W10390580": "COOKTOPS",
    "W10544749": "LAVA LOUÇAS",
    "W10901034": "PURIFICADORES",
    "W11198953": "PURIFICADORES",
    "W10301562": "PURIFICADORES",
    "W11100357": "FORNOS",
    "W10866789": "FOGÕES",
    "W11360276": "REFRIGERADORES",
    "W10287547": "REFRIGERADORES",
    "W10287527": "REFRIGERADORES",
    "W11132264": "FOGÕES",
    "W10320833": "REFRIGERADORES",
    "W10351342": "REFRIGERADORES",
    "W10638541": "COIFAS / DEPURADORES",
    "W11300695": "LAVADORAS",
    "W11100356": "FOGÕES",
    "W10915770": "FORNOS",
    "326075876": "COIFAS / DEPURADORES",
    "326073242": "ADEGA",
    "W10455522": "REFRIGERADORES",
    "W10638550": "COIFAS / DEPURADORES",
    "W11245249": "LAVADORAS",
    "W10806305": "FORNOS",
    "W10544738": "LAVA LOUÇAS",
}

SKU_SUBFAMILY_CORRECTIONS = {
    "W10894262": "Manípulos",
    "W10686098": "Manípulos",
    "W10907728": "Acessórios",
    "W10705755": "Acessórios",
    "W10420716": "Acessórios",
    "W11100356": "Acessórios",
    "W10577330": "Tampas Móveis",
    "W11393157": "Tampas Móveis",
    "W10644162": "Tampas Móveis",
    "W11185123": "Tampas Móveis",
    "W10866791": "Kits Instalação",
    "W10866790": "Kits Instalação",
    "W11559186": "Kits Instalação",
    "W10866789": "Kits Instalação",
    "W11047054": "Interruptores horário",
    "W11245249": "Válvulas de Água",
}


def seed_sku_business_metadata(db):
    skus = list(SKU_BUSINESS_METADATA)
    existing = {
        item.sku: item
        for item in db.query(KnownSku).filter(KnownSku.sku.in_(skus)).all()
    }
    now = datetime.utcnow()
    for sku, (cluster, _portfolio, subfamily) in SKU_BUSINESS_METADATA.items():
        item = existing.get(sku)
        if item is None:
            item = KnownSku(sku=sku)
            db.add(item)
        if cluster.startswith("CONSUM"):
            cluster = "CONSUMÍVEIS & ACESSÓRIOS"
        elif cluster.startswith("PE"):
            cluster = "PEÇAS+"
        if not item.cluster:
            item.cluster = cluster
        if not item.category:
            item.category = SKU_CATEGORIES[sku]
        item.portfolio = None
        if not item.subfamily:
            item.subfamily = SKU_SUBFAMILY_CORRECTIONS.get(sku, subfamily)
        if db.is_modified(item) or item in db.new:
            item.updated_at = now
    db.commit()

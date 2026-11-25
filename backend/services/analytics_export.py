from collections import defaultdict
from datetime import datetime, timedelta, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from services.structured_products import offer_available, seller_scope_key


HEADER_FILL = PatternFill("solid", fgColor="E9EEF5")
HEADER_FONT = Font(name="Aptos", size=11, bold=True, color="172033")
BODY_FONT = Font(name="Aptos", size=10, color="253047")
LINK_FONT = Font(name="Aptos", size=10, color="087E8B", underline="single")
THIN_BORDER = Border(bottom=Side(style="thin", color="DDE3EC"))
CURRENCY_FORMAT = 'R$ #,##0.00'
PERCENT_FORMAT = '0.00%'


def _excel_datetime(value):
    if value is None:
        return None
    utc_value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return utc_value.astimezone(timezone(timedelta(hours=-3))).replace(tzinfo=None)


def _style_sheet(sheet, widths):
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[1].height = 25

    for cell in sheet[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center")
        cell.border = THIN_BORDER

    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.font = BODY_FONT
            cell.alignment = Alignment(vertical="center")
            cell.border = THIN_BORDER

    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width


NOT_INFORMED = 'Não informado'


def export_value(value):
    return NOT_INFORMED if value is None or isinstance(value, str) and not value.strip() else value


def _write_analytics_sheet(workbook, rows):
    sheet = workbook.active
    sheet.title = "Analytics"
    sheet.append(
        [
            "SKU",
            "Produto",
            "Descrição",
            "Subfamília",
            "Categoria",
            "Cluster",
            "Mercado",
            "Lojas",
            "Ofertas",
            "Preço médio",
            "Melhor preço",
            "Melhor lojista",
            "Diferença",
            "Diferença (%)",
            "Maior preço",
            "Meu preço",
            "Meu preço - melhor preço",
            "Código interno",
            "ID externo",
        ]
    )

    for row in rows:
        sheet.append(
            [
                row.sku or "",
                row.name,
                row.description or "",
                row.subfamily or "",
                row.sku_category or "",
                row.cluster or "",
                row.market_type or "",
                row.store_count,
                row.offer_count,
                row.pma,
                row.cheapest_price,
                row.cheapest_store,
                row.difference_value,
                abs(row.difference_percent) / 100 if row.difference_percent is not None else None,
                row.highest_price,
                row.reference_price,
                row.reference_difference,
                row.internal_code,
                row.external_id,
            ]
        )

    _style_sheet(sheet, [16, 45, 38, 22, 22, 16, 16, 11, 11, 17, 17, 28, 17, 18, 17, 17, 25, 20, 20])
    for row_index in range(2, sheet.max_row + 1):
        for column in (10, 11, 13, 15, 16, 17):
            sheet.cell(row_index, column).number_format = CURRENCY_FORMAT
        sheet.cell(row_index, 14).number_format = PERCENT_FORMAT


def _write_stores_sheet(workbook, rows, date_order=None):
    stats = defaultdict(
        lambda: {
            "source": "",
            "seller_name": "",
            "prices": [],
            "skus": set(),
            "products": set(),
            "last_update": None,
            "shipping_costs": [],
            "lead_times": [],
        }
    )

    for row in rows:
        for offer in row.offers:
            if not offer_available({"availability": offer.availability}):
                continue
            store = stats[seller_scope_key({"source": offer.source, "store": offer.store})]
            store["seller_name"] = offer.store
            last_update = _excel_datetime(offer.last_update)
            store["source"] = offer.source or offer.store
            if offer.current_price is not None:
                store["prices"].append(offer.current_price)
            store["skus"].add(row.sku or "")
            store["products"].add(row.name)
            if offer.shipping_cost is not None:
                store["shipping_costs"].append(offer.shipping_cost)
            if offer.delivery_max_days is not None:
                store["lead_times"].append(offer.delivery_max_days)
            if last_update and (
                store["last_update"] is None or last_update > store["last_update"]
            ):
                store["last_update"] = last_update

    sheet = workbook.create_sheet("Lojistas")
    sheet.append(
        [
            "Lojista",
            "Fonte",
            "Ofertas",
            "Produtos",
            "SKUs",
            "Melhor preço",
            "Preço médio",
            "Frete médio",
            "Lead time médio (dias)",
            "Última atualização",
        ]
    )

    ordered = sorted(
        stats.items(),
        key=lambda item: (-len(item[1]["prices"]), min(item[1]["prices"])),
    )
    if date_order:
        ordered.sort(key=lambda item: item[1]["last_update"] or
                     (datetime.max if date_order == 'asc' else datetime.min), reverse=date_order == 'desc')
    for _store_key, store in ordered:
        prices = store["prices"]
        sheet.append(
            [
                store["seller_name"],
                store["source"],
                len(prices),
                len(store["products"]),
                len({sku for sku in store["skus"] if sku}),
                min(prices),
                sum(prices) / len(prices),
                (
                    sum(store["shipping_costs"]) / len(store["shipping_costs"])
                    if store["shipping_costs"]
                    else None
                ),
                (
                    sum(store["lead_times"]) / len(store["lead_times"])
                    if store["lead_times"]
                    else None
                ),
                store["last_update"],
            ]
        )

    _style_sheet(sheet, [30, 22, 11, 12, 10, 17, 17, 17, 23, 21])
    for row_index in range(2, sheet.max_row + 1):
        sheet.cell(row_index, 6).number_format = CURRENCY_FORMAT
        sheet.cell(row_index, 7).number_format = CURRENCY_FORMAT
        sheet.cell(row_index, 8).number_format = CURRENCY_FORMAT
        sheet.cell(row_index, 9).number_format = '0.0 "dias"'
        sheet.cell(row_index, 10).number_format = "dd/mm/yyyy hh:mm"


def _write_offers_sheet(workbook, rows, date_order=None):
    sheet = workbook.create_sheet("Ofertas")
    sheet.append(
        [
            "SKU",
            "Produto",
            "Descrição",
            "Subfamília",
            "Categoria",
            "Cluster",
            "Mercado",
            "Lojista",
            "Fonte",
            "Preço",
            "À vista",
            "A prazo",
            "Parcelas",
            "Valor da parcela",
            "Diferença pagamento",
            "Frete",
            "Total com frete",
            "Prazo mínimo (dias)",
            "Prazo máximo (dias)",
            "Método de envio",
            "CEP",
            "Disponibilidade",
            "Origem logística",
            "Descoberta por",
            "Cache",
            "Atualizado em",
            "URL",
            "Tipo de prazo",
            "Logística 2P/3P (2P plataforma; 3P lojista)",
            "Entregue por",
            "Evidência logística",
        ]
    )

    ranked_offers = [
        (row, offer) for row in rows
        for offer in sorted(row.offers, key=lambda item: item.current_price if item.current_price is not None else float('inf'))
    ]
    if date_order:
        ranked_offers.sort(key=lambda pair: pair[0].sku or "")
        ranked_offers.sort(
            key=lambda pair: _excel_datetime(pair[1].last_update) or
            (datetime.max if date_order == 'asc' else datetime.min),
            reverse=date_order == 'desc',
        )
    for row, offer in ranked_offers:
        cash_price = offer.cash_price or offer.current_price
        total_with_shipping = (
            cash_price + offer.shipping_cost
            if offer.shipping_cost is not None and cash_price is not None
            else None
        )
        sheet.append(
            [
                row.sku or "",
                row.name,
                row.description or "",
                row.subfamily or "",
                row.sku_category or "",
                row.cluster or "",
                row.market_type or "",
                offer.store,
                offer.source or offer.store,
                offer.current_price if offer_available({"availability": offer.availability}) else "Sem estoque",
                cash_price,
                offer.installment_price,
                offer.installment_count,
                offer.installment_value,
                offer.payment_difference,
                offer.shipping_cost,
                total_with_shipping,
                offer.delivery_min_days,
                offer.delivery_max_days,
                offer.shipping_method or "",
                offer.destination_zip or "",
                "Sem estoque" if not offer_available({'availability': offer.availability}) else offer.availability or "",
                offer.logistics_source or "",
                offer.discovery_source or "",
                "Sim" if offer.is_cached else "Não",
                _excel_datetime(offer.last_update),
                offer.url,
                {"business": "Dias uteis", "calendar": "Dias corridos"}.get(offer.delivery_days_type, "Nao informado"),
                offer.fulfillment_type or "",
                offer.fulfilled_by or "",
                offer.fulfillment_evidence or "",
            ]
        )
        link_cell = sheet.cell(sheet.max_row, 27)
        link_cell.hyperlink = offer.url
        link_cell.font = LINK_FONT

    _style_sheet(
        sheet,
        [16, 45, 38, 22, 22, 16, 16, 30, 22, 17, 17, 17, 10, 18, 20, 15, 18, 20, 20, 24, 13, 18, 20, 18, 11, 21, 62, 18, 44, 28, 60],
    )
    for row_index in range(2, sheet.max_row + 1):
        for column in (10, 11, 12, 14, 15, 16, 17):
            sheet.cell(row_index, column).number_format = CURRENCY_FORMAT
        sheet.cell(row_index, 26).number_format = "dd/mm/yyyy hh:mm"
        sheet.cell(row_index, 27).font = LINK_FONT


def build_analytics_workbook(payload, *, date_order=None):
    workbook = Workbook()
    _write_analytics_sheet(workbook, payload.rows)
    _write_stores_sheet(workbook, payload.rows, date_order)
    _write_offers_sheet(workbook, payload.rows, date_order)
    for sheet in workbook:
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.value = export_value(cell.value)

    workbook.properties.title = "NeveTracker Analytics"
    workbook.properties.subject = payload.query or "Dados de analytics e lojistas"
    workbook.properties.creator = "NeveTracker"
    workbook.properties.created = datetime.now()

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer

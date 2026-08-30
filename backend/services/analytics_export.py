from collections import defaultdict
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


HEADER_FILL = PatternFill("solid", fgColor="E9EEF5")
HEADER_FONT = Font(name="Aptos", size=11, bold=True, color="172033")
BODY_FONT = Font(name="Aptos", size=10, color="253047")
LINK_FONT = Font(name="Aptos", size=10, color="087E8B", underline="single")
THIN_BORDER = Border(bottom=Side(style="thin", color="DDE3EC"))
CURRENCY_FORMAT = 'R$ #,##0.00'
PERCENT_FORMAT = '0.00%'


def _excel_datetime(value):
    if value and value.tzinfo:
        return value.astimezone().replace(tzinfo=None)
    return value


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
                abs(row.difference_percent) / 100,
            ]
        )

    _style_sheet(sheet, [16, 45, 38, 22, 22, 16, 16, 11, 11, 17, 17, 28, 17, 18])
    for row_index in range(2, sheet.max_row + 1):
        for column in (10, 11, 13):
            sheet.cell(row_index, column).number_format = CURRENCY_FORMAT
        sheet.cell(row_index, 14).number_format = PERCENT_FORMAT


def _write_stores_sheet(workbook, rows):
    stats = defaultdict(
        lambda: {
            "source": "",
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
            store = stats[offer.store]
            last_update = _excel_datetime(offer.last_update)
            store["source"] = offer.source or offer.store
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
    for store_name, store in ordered:
        prices = store["prices"]
        sheet.append(
            [
                store_name,
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


def _write_offers_sheet(workbook, rows):
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
        ]
    )

    for row in rows:
        for offer in sorted(row.offers, key=lambda item: item.current_price):
            cash_price = offer.cash_price or offer.current_price
            total_with_shipping = (
                cash_price + offer.shipping_cost
                if offer.shipping_cost is not None
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
                    offer.current_price,
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
                    offer.availability or "",
                    offer.logistics_source or "",
                    offer.discovery_source or "",
                    "Sim" if offer.is_cached else "Não",
                    _excel_datetime(offer.last_update),
                    offer.url,
                ]
            )
            link_cell = sheet.cell(sheet.max_row, 27)
            link_cell.hyperlink = offer.url
            link_cell.font = LINK_FONT

    _style_sheet(
        sheet,
        [16, 45, 38, 22, 22, 16, 16, 30, 22, 17, 17, 17, 10, 18, 20, 15, 18, 20, 20, 24, 13, 18, 20, 18, 11, 21, 62],
    )
    for row_index in range(2, sheet.max_row + 1):
        for column in (10, 11, 12, 14, 15, 16, 17):
            sheet.cell(row_index, column).number_format = CURRENCY_FORMAT
        sheet.cell(row_index, 26).number_format = "dd/mm/yyyy hh:mm"
        sheet.cell(row_index, 27).font = LINK_FONT


def build_analytics_workbook(payload):
    workbook = Workbook()
    _write_analytics_sheet(workbook, payload.rows)
    _write_stores_sheet(workbook, payload.rows)
    _write_offers_sheet(workbook, payload.rows)

    workbook.properties.title = "NeveTracker Analytics"
    workbook.properties.subject = payload.query or "Dados de analytics e lojistas"
    workbook.properties.creator = "NeveTracker"
    workbook.properties.created = datetime.now()

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer

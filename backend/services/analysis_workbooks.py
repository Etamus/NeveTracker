from io import BytesIO
from copy import copy
from datetime import date, datetime
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter
from services.analytics_export import _style_sheet, _excel_datetime, export_value, HEADER_FILL, HEADER_FONT, BODY_FONT, THIN_BORDER


def safe(value):
    return "'"+value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value


def append(sheet, values):
    if getattr(sheet.parent, 'show_missing_values', False):
        values = [export_value(value) for value in values]
    if sheet.parent.write_only:
        _append_streaming(sheet, values)
        return
    sheet.append([safe(value) for value in values])


def _append_streaming(sheet, values):
    """Style rows as they are written instead of retaining the full export."""
    book=sheet.parent
    if not hasattr(book, '_report_styles'):
        body=WriteOnlyCell(sheet);body.font=BODY_FONT;body.alignment=Alignment(vertical='center');body.border=THIN_BORDER
        header=WriteOnlyCell(sheet);header._style=copy(body._style);header.font=HEADER_FONT;header.fill=HEADER_FILL
        decimal=WriteOnlyCell(sheet);decimal._style=copy(body._style);decimal.number_format='#,##0.00'
        timestamp=WriteOnlyCell(sheet);timestamp._style=copy(body._style);timestamp.number_format='yyyy-mm-dd h:mm:ss'
        day=WriteOnlyCell(sheet);day._style=copy(body._style);day.number_format='yyyy-mm-dd'
        book._report_styles={key:cell._style for key,cell in [('body',body),('header',header),('decimal',decimal),('datetime',timestamp),('date',day)]}
    first=not getattr(sheet, 'max_row', 0)
    sheet.max_row=getattr(sheet,'max_row',0)+1
    sheet.max_column=max(getattr(sheet,'max_column',0),len(values))
    if first:
        sheet.freeze_panes='A2';sheet.sheet_view.showGridLines=False;sheet.row_dimensions[1].height=25
        for index in range(1,len(values)+1):sheet.column_dimensions[get_column_letter(index)].width=20
    cells=[]
    for value in values:
        cell=WriteOnlyCell(sheet,value=safe(value))
        kind='header' if first else 'datetime' if isinstance(value,datetime) else 'date' if isinstance(value,date) else 'decimal' if isinstance(value,float) else 'body'
        cell._style=book._report_styles[kind]
        cells.append(cell)
    sheet.append(cells)


def finish(book):
    for sheet in book:
        if book.write_only:
            sheet.auto_filter.ref=f'A1:{get_column_letter(sheet.max_column)}{sheet.max_row}'
            continue
        _style_sheet(sheet, [20]*sheet.max_column)
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                if isinstance(cell.value, float):
                    cell.number_format = '#,##0.00'
    output = BytesIO(); book.save(output); output.seek(0)
    return output


STATUS = {'quoted': 'Cotado', 'mixed': 'Prazos distintos', 'out_of_stock': 'Sem estoque',
          'invalid_postal_code': 'CEP não localizado',
          'queued': 'Na fila', 'running': 'Consultando', 'not_quoted': 'Não informado',
          'not_queried': 'Não consultado', 'failed': 'Falha', 'no_offers': 'Sem ofertas', 'cancelled': 'Cancelado'}


def lead_time_workbook(matrix, region=None):
    book = Workbook(); wide = book.active; wide.title = 'Matriz de prazos'
    book.show_missing_values = True
    details = book.create_sheet('Cotações por lojista')
    destinations = [dest for dest in matrix['destinations'] if not region or dest['region'] == region]
    append(wide, ['SKU', 'Loja', 'Lojista', *[f"{dest['uf']} · {dest['capital']} · {dest['postal_code']}" for dest in destinations]])
    append(details, ['SKU', 'Loja', 'Lojista', 'UF', 'Cidade', 'CEP', 'Status', 'Prazo mínimo', 'Prazo máximo',
                     'Tipo de dias', 'Frete', 'Serviço', 'Cotado em (Brasília)', 'Oferta', 'Mais de 24h'])
    for store in matrix['stores']:
        for seller in [{'name': 'Resumo da loja', 'cells': store['cells']}, *store['sellers']]:
            values = []
            for dest in destinations:
                cell = seller['cells'].get(dest['key'], {'status': 'not_queried'})
                value = STATUS.get(cell['status'], cell['status'])
                low, high = cell.get('delivery_min_days'), cell.get('delivery_max_days')
                if (low is not None or high is not None) and cell['status'] == 'quoted':
                    unit = {'business': 'dias úteis', 'calendar': 'dias corridos'}.get(cell.get('delivery_days_type'), 'dias (tipo não informado)')
                    if high is None:
                        span = f"A partir de {low}"
                    elif low is None:
                        span = f"Até {high}"
                    else:
                        span = str(low) if low == high else f"{low}–{high}"
                    value = f"{span} {unit}"
                    if cell.get('shipping_cost') is not None:
                        value += f" | Frete {cell['shipping_cost']:.2f}"
                values.append(value)
                if seller['name'] == 'Resumo da loja' and store['sellers']:
                    continue
                for option in cell.get('alternatives') or [cell]:
                    append(details, [matrix['sku'], store['name'], seller['name'], dest['uf'], dest['capital'], dest['postal_code'],
                        STATUS.get(option['status'], option['status']), option.get('delivery_min_days'), option.get('delivery_max_days'),
                        {'business': 'Úteis', 'calendar': 'Corridos'}.get(option.get('delivery_days_type'), 'Não informado'),
                        option.get('shipping_cost'), option.get('shipping_method'), _excel_datetime(option.get('captured_at')),
                        option.get('url'), 'Sim' if option.get('stale') else 'Não'])
            append(wide, [matrix['sku'], store['name'], seller['name'], *values])
    return finish(book)


def history_workbook(report):
    book = Workbook(); sheet = book.active; sheet.title = 'Histórico diário'
    book.show_missing_values = True
    append(sheet, ['Data', 'SKU', 'Produto', 'Marca', 'Loja', 'Lojista', 'Preço', 'Dia anterior', 'Diferença', 'Variação (%)',
                   'Frete', 'CEP', 'Disponibilidade', 'Coletas no dia', 'Mudou no dia', 'Origem do valor', 'Observado em (Brasília)', 'Oferta',
                   'Prazo mínimo', 'Prazo máximo', 'Tipo de dias', 'Preço à vista', 'Preço parcelado', 'Parcelas', 'Valor da parcela',
                   'Entrega', 'Entregue por', 'Campos alterados', 'Eventos de estoque', 'Observações anteriores (Brasília)'])
    for row in report['items']:
        append(sheet, [row['date'], row['sku'], row['name'], row['brand'], row['source'], row['seller'], row['price'],
            row['previous_price'], row['difference'], row['difference_percent'], row['shipping_cost'], row['postal_code'],
            row['availability'], row['checks'], 'Sim' if row['changed'] else 'Não',
            'Último valor conhecido' if row['carried'] else 'Observação do dia', _excel_datetime(row['observed_at']), row['url'],
            row['delivery_min_days'], row['delivery_max_days'], row['delivery_days_type'], row['details'].get('cash_price'),
            row['details'].get('installment_price'), row['details'].get('installment_count'), row['details'].get('installment_value'),
            row['details'].get('fulfillment_type'), row['details'].get('fulfilled_by'), ', '.join(row['changed_fields']),
            ' | '.join(f"{'Ruptura' if event['kind'] == 'rupture' else 'Reposição'} em "
                       f"{_excel_datetime(event['observed_at']):%d/%m/%Y %H:%M}" for event in row.get('stock_events', ())),
            ' | '.join(f"{_excel_datetime(event['previous_observed_at']):%d/%m/%Y %H:%M}"
                       for event in row.get('stock_events', ()))])
    series = book.create_sheet('Resumo diário')
    append(series, ['Data', 'Mínimo', 'Média', 'Máximo', 'Ofertas', 'Alterações'])
    for row in report['series']:
        append(series, [row['date'], row['minimum'], row['average'], row['maximum'], row['offers'], row['changes']])
    return finish(book)


def comparison_workbook(result):
    book = Workbook(); sheet = book.active; sheet.title = 'Comparação'
    book.show_missing_values = True
    append(sheet, ['Grupo', 'SKUs', 'Lojistas', 'Ofertas', 'Disponíveis', 'Mínimo', 'Média', 'Última observação (Brasília)'])
    for row in result['groups']:
        append(sheet, [row['label'], row['skus'], row['sellers'], row['offers'], row['available'], row['minimum'], row['average'], _excel_datetime(row['last_update'])])
    common = book.create_sheet('Por SKU')
    append(common, ['SKU', result['groups'][0]['label'], result['groups'][1]['label'], 'Diferença B − A'])
    for row in result['items']:
        append(common, [row['sku'], row['left'], row['right'], row['difference']])
    return finish(book)

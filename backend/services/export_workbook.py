"""Sequential XLSX writer for the large, complete export (bounded worksheet memory)."""
from datetime import date, datetime
from io import BytesIO

import xlsxwriter

from services.analysis_workbooks import safe
from services.analytics_export import export_value


class ExportWorkbook:
    def __init__(self):
        self.output = BytesIO()
        self.writer = xlsxwriter.Workbook(self.output, {
            'constant_memory': True, 'strings_to_urls': False,
            'strings_to_formulas': False, 'nan_inf_to_errors': True,
        })
        base = {'font_name': 'Aptos', 'font_size': 10, 'font_color': '#253047',
                'valign': 'vcenter', 'bottom': 1, 'bottom_color': '#DDE3EC'}
        self.formats = {kind: self.writer.add_format({**base, **extra}) for kind, extra in {
            'body': {}, 'header': {'bold': True, 'font_size': 11, 'font_color': '#172033', 'bg_color': '#EEF2F6'},
            'decimal': {'num_format': '#,##0.00'}, 'datetime': {'num_format': 'yyyy-mm-dd h:mm:ss'},
            'date': {'num_format': 'yyyy-mm-dd'},
        }.items()}
        self.sheets = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.finish()

    def create_sheet(self, title):
        sheet = ExportSheet(self, title)
        self.sheets.append(sheet)
        return sheet

    def finish(self):
        if not self.closed:
            for sheet in self.sheets:
                if sheet.max_row and sheet.max_column:
                    sheet.writer.autofilter(0, 0, sheet.max_row - 1, sheet.max_column - 1)
            self.writer.close()
            self.closed = True
        self.output.seek(0)
        return self.output


class ExportSheet:
    def __init__(self, book, title):
        self.book, self.title = book, title
        self.writer = book.writer.add_worksheet(title)
        self.max_row = self.max_column = 0
        self.writer.freeze_panes(1, 0)
        self.writer.hide_gridlines(2)

    def append(self, values):
        first = self.max_row == 0
        if first:
            self.writer.set_row(0, 25)
            self.writer.set_column(0, len(values) - 1, 20)
        for column, value in enumerate(values):
            value = safe(export_value(value))
            kind = 'header' if first else 'datetime' if isinstance(value, datetime) else 'date' if isinstance(value, date) else 'decimal' if isinstance(value, float) else 'body'
            style = self.book.formats[kind]
            if isinstance(value, str):
                self.writer.write_string(self.max_row, column, value, style)
            elif isinstance(value, (datetime, date)):
                self.writer.write_datetime(self.max_row, column, value, style)
            elif isinstance(value, bool):
                self.writer.write_boolean(self.max_row, column, value, style)
            elif isinstance(value, (int, float)):
                self.writer.write_number(self.max_row, column, value, style)
            elif value is not None:
                self.writer.write(self.max_row, column, value, style)
        self.max_row += 1
        self.max_column = max(self.max_column, len(values))

    def add_price_chart(self):
        if self.max_row <= 1:
            return
        last = min(self.max_row, 31) - 1
        chart = self.book.writer.add_chart({'type': 'column'})
        chart.set_title({'name': 'Preço mínimo por loja (dados por SKU e CEP)'})
        chart.add_series({'name': [self.title, 0, 5], 'categories': [self.title, 1, 2, last, 2], 'values': [self.title, 1, 5, last, 5]})
        self.writer.insert_chart('J2', chart)


def append(sheet, values):
    sheet.append(values)


def finish(book):
    return book.finish()

from datetime import date, datetime
from io import BytesIO
import unittest

from openpyxl import load_workbook

from services.export_workbook import ExportWorkbook, append, finish


class ExportWorkbookTests(unittest.TestCase):
    def test_streaming_writer_preserves_types_safety_filter_and_column_widths(self):
        with ExportWorkbook() as book:
            sheet = book.create_sheet('Dados')
            append(sheet, ['SKU', 'Preço', 'Data', 'Dia', 'Verificado', 'Observação', 'URL'])
            append(sheet, ['00123', 12.5, datetime(2026,11,17,12,30), date(2026,11,17), True, '=1+1', 'https://example.test/item'])
            append(sheet, ['00124', 0.0, None, None, False, 'Controle\x01 de origem', None])
            output = finish(book).getvalue()
        loaded = load_workbook(BytesIO(output))['Dados']
        self.assertEqual(loaded['A2'].value, '00123')
        self.assertEqual(loaded['B2'].value, 12.5)
        self.assertEqual(loaded['B2'].number_format, '#,##0.00')
        self.assertEqual(loaded['C2'].value, datetime(2026,11,17,12,30))
        self.assertTrue(loaded['D2'].is_date)
        self.assertIs(loaded['E2'].value, True)
        self.assertEqual(loaded['F2'].value, "'=1+1")
        self.assertNotEqual(loaded['F2'].data_type, 'f')
        self.assertIsNone(loaded['G2'].hyperlink)
        self.assertEqual(loaded['B3'].value, 0)
        self.assertIs(loaded['E3'].value, False)
        self.assertEqual(loaded.freeze_panes, 'A2')
        self.assertEqual(loaded.auto_filter.ref, 'A1:G3')

    def test_writer_keeps_all_rows_across_batch_boundaries_and_closes_after_errors(self):
        with ExportWorkbook() as book:
            sheet = book.create_sheet('Dados')
            append(sheet,['SKU','CEP','Preço'])
            for index in range(1501): append(sheet,[f'SKU{index}','01001000',float(index)])
            output=finish(book).getvalue()
        loaded=load_workbook(BytesIO(output),read_only=True)['Dados']
        rows=list(loaded.iter_rows(values_only=True))
        self.assertEqual(len(rows),1502)
        self.assertEqual(rows[-1],('SKU1500','01001000',1500))
        with self.assertRaises(ValueError):
            with ExportWorkbook() as interrupted:
                append(interrupted.create_sheet('Parcial'),['SKU'])
                raise ValueError('interrupted')
        self.assertTrue(interrupted.closed)


if __name__ == '__main__':
    unittest.main()

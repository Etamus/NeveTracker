"""Allow a confirmed unavailable offer to have no published price."""

import re


def ensure_nullable_price(engine):
    if engine.dialect.name != 'sqlite':
        if engine.dialect.name == 'postgresql':
            with engine.begin() as connection:
                connection.exec_driver_sql('ALTER TABLE price_history ALTER COLUMN price DROP NOT NULL')
        return
    with engine.begin() as connection:
        columns = connection.exec_driver_sql('PRAGMA table_info(price_history)').fetchall()
        if not any(row[1] == 'price' and row[3] for row in columns):
            return
        original = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='table' AND name='price_history'").scalar_one()
        indexes = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='price_history' AND sql IS NOT NULL").scalars().all()
        nullable, replacements = re.subn(r'(?i)("?price"?\s+FLOAT)\s+NOT\s+NULL', r'\1', original, count=1)
        if replacements != 1:
            raise RuntimeError('Nao foi possivel migrar a coluna de preco sem preservar o esquema.')
        temporary = re.sub(r'(?i)CREATE TABLE\s+"?price_history"?', 'CREATE TABLE price_history_nullable', nullable, count=1)
        names = ','.join('"' + row[1].replace('"', '""') + '"' for row in columns)
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        connection.exec_driver_sql(temporary)
        connection.exec_driver_sql(f'INSERT INTO price_history_nullable ({names}) SELECT {names} FROM price_history')
        connection.exec_driver_sql('DROP TABLE price_history')
        connection.exec_driver_sql('ALTER TABLE price_history_nullable RENAME TO price_history')
        for sql in indexes:
            connection.exec_driver_sql(sql)

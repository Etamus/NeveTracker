from datetime import datetime
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session
from database import get_db
from models.product import KnownSku
from services.auth_service import require_developer
import re

sku_metadata_router = APIRouter(dependencies=[Depends(require_developer)])


def saved_skus(db):
    from models.product import Product
    return {sku for (sku,) in db.query(KnownSku.sku).union(db.query(Product.sku)) if sku}


class MetadataRow(BaseModel):
    sku: str = Field(min_length=1, max_length=80)
    category: str | None = Field(default=None, max_length=250)
    cluster: str | None = Field(default=None, max_length=250)
    subfamily: str | None = Field(default=None, max_length=250)
    market_type: Literal['Cativo', 'Competitivo', ''] | None = None

    @field_validator('sku')
    @classmethod
    def normalized_sku(cls, value):
        value=value.strip().upper()
        if not re.fullmatch(r'[A-Z0-9._-]+', value):
            raise ValueError('SKU inválido.')
        return value


class MetadataRequest(BaseModel):
    items: list[MetadataRow] = Field(min_length=1, max_length=5000)


@sku_metadata_router.put('')
def save_metadata(payload: MetadataRequest, db: Session = Depends(get_db)):
    skus=[item.sku for item in payload.items]
    if len(set(skus)) != len(skus):
        raise HTTPException(400, 'Existem SKUs duplicados no preenchimento.')
    existing = {row.sku: row for row in db.query(KnownSku).filter(KnownSku.sku.in_(skus))}
    missing = [sku for sku in skus if sku not in existing]
    unknown = set(missing)-saved_skus(db) if missing else set()
    if unknown:
        raise HTTPException(404, 'SKU não encontrado no banco: ' + ', '.join(sorted(unknown)))
    for sku in missing:
        existing[sku]=KnownSku(sku=sku);db.add(existing[sku])
    for item in payload.items:
        row=existing[item.sku]
        for field in item.model_fields_set-{'sku'}:
            value=getattr(item,field)
            setattr(row,field,value.strip() or None if isinstance(value,str) else value)
        row.updated_at=datetime.utcnow()
    db.commit()
    return {'updated_count':len(skus), 'skus':skus}


@sku_metadata_router.get('/template')
def metadata_template(db: Session = Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.worksheet.datavalidation import DataValidation
    from services.analysis_workbooks import append, finish
    from routes.analysis import excel
    book=Workbook(); sheet=book.active; sheet.title='Cadastro de SKUs'
    append(sheet,['SKU','Categoria','Cluster','Subfamilia','Mercado'])
    rows={row.sku:row for row in db.query(KnownSku)}
    for sku in sorted(saved_skus(db)):
        row=rows.get(sku)
        append(sheet,[sku, *[getattr(row,field,None) for field in ('category','cluster','subfamily','market_type')]])
    validation=DataValidation(type='list',formula1='"Cativo,Competitivo"',allow_blank=True)
    sheet.add_data_validation(validation);validation.add(f'E2:E{max(sheet.max_row,5001)}')
    return excel(finish(book),'modelo-cadastro-skus.xlsx')


@sku_metadata_router.post('/import')
async def import_metadata(file: UploadFile = File(...), db: Session = Depends(get_db)):
    from routes.products import _tabular_import_rows, _normalize_market_type, SKU_IMPORT_MAX_BYTES
    from pydantic import ValidationError
    data=await file.read(SKU_IMPORT_MAX_BYTES+1)
    filename=file.filename or '';await file.close()
    if len(data)>SKU_IMPORT_MAX_BYTES:
        raise HTTPException(413,'O arquivo excede o limite de 8 MB.')
    from zipfile import BadZipFile
    from openpyxl.utils.exceptions import InvalidFileException
    from xml.etree.ElementTree import ParseError
    try:
        rows=_tabular_import_rows(filename,data)
    except (BadZipFile, InvalidFileException, ParseError, ValueError, OSError, KeyError):
        raise HTTPException(400,'A planilha está inválida ou corrompida. Baixe o modelo e tente novamente.')
    items=[]
    for line,values in rows:
        item={'sku':values.get('sku','')}
        for field in ('category','cluster','subfamily','market_type'):
            value=values.get(field,'').strip()
            if value:
                if field=='market_type':
                    value=_normalize_market_type(value)
                    if not value:raise HTTPException(400,f'Linha {line}: Mercado deve ser Cativo ou Competitivo.')
                item[field]=value
        items.append(item)
    try:
        payload=MetadataRequest(items=items)
    except ValidationError:
        raise HTTPException(400,'Confira os SKUs, os campos e o limite de 5.000 linhas da planilha.')
    return save_metadata(payload,db)

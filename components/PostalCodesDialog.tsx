import DialogBackdrop from "./DialogBackdrop";
import { useState } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import SpreadsheetInput from './SpreadsheetInput';
import ClearIcon from './ClearIcon';

export default function PostalCodesDialog({ values, onSave, onClose }: { values: string[]; onSave: (values: string[]) => void; onClose: () => void }) {
  const [text,setText]=useState(values.join('\n'));
  const [error,setError]=useState('');
  const save=()=>{
    const codes=[...new Set(text.split(/[\s,;]+/).filter(Boolean))];
    if(codes.length>200 || codes.some(code=>!/^\d{5}-?\d{3}$/.test(code))){setError('Informe até 200 CEPs válidos com oito dígitos.');return;}
    onSave(codes.map(code=>code.replace('-','')));onClose();
  };
  return createPortal(<DialogBackdrop aria-label="CEPs de consulta" onDismiss={onClose}><section className="workspace-dialog workspace-dialog--postal-codes">
    <div className="dialog-header"><div><h2>CEPs de consulta</h2></div><button className="icon-only-button icon-button" type="button" aria-label="Fechar" onClick={onClose}><X size={17}/></button></div>
    <div className="postal-dialog-content"><SpreadsheetInput label="CEPs" kind="postal" value={text} onChange={setText} action={<button type="button" className="icon-only-button icon-button" aria-label="Limpar CEPs" data-tooltip="Limpar CEPs" onClick={()=>{setText('');setError('');}}><ClearIcon /></button>}/>{error&&<p className="auth-error" role="alert">{error}</p>}</div>
    <div className="sku-selector-footer"><button type="button" className="button-secondary" onClick={()=>{onSave([]);onClose();}}>Usar padrão</button><button type="button" className="button-primary" onClick={save}>Salvar CEPs</button></div>
  </section></DialogBackdrop>,document.body);
}

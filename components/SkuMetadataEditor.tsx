import { useState } from 'react';
import { createPortal } from 'react-dom';
import { CheckCircle2, FileDown, LoaderCircle, Upload } from 'lucide-react';
import { StyledSelect } from './StyledSelect';
import { getApiBaseUrl } from './HeroSection/viewModel';
import { downloadExcel } from '../src/download';
import type { SkuStatusItem } from '../models/product.model';

export default function SkuMetadataEditor({ items, headers, onSaved, footerContainer }: { items: SkuStatusItem[]; headers: (initial?: HeadersInit) => HeadersInit; onSaved: () => void | Promise<void>; footerContainer: HTMLElement | null }) {
  const [sku, setSku] = useState('');
  const [fields, setFields] = useState({ category: '', cluster: '', subfamily: '', market_type: '' });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const selectSku = (value: string) => {
    setSku(value);
    const item = items.find((row) => row.sku === value);
    setFields({ category: item?.sku_category || '', cluster: item?.cluster || '', subfamily: item?.subfamily || '', market_type: item?.market_type || '' });
    setMessage(''); setError('');
  };
  const save = async (file?: File) => {
    setBusy(true); setError(''); setMessage('');
    try {
      let response: Response;
      if (!file) {
        if (!items.some((item) => item.sku === sku)) throw new Error('Selecione um SKU do banco.');
        response = await fetch(`${getApiBaseUrl()}/products/skus/metadata`, { method: 'PUT', headers: headers({ 'Content-Type': 'application/json' }), body: JSON.stringify({ items: [{ sku, ...fields }] }) });
      } else {
        const form = new FormData(); form.append('file', file);
        response = await fetch(`${getApiBaseUrl()}/products/skus/metadata/import`, { method: 'POST', headers: headers(), body: form });
      }
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Não foi possível salvar os dados.');
      setMessage(`${result.updated_count} SKU(s) salvo(s).`);
      if (file) { setSku(''); setFields({ category: '', cluster: '', subfamily: '', market_type: '' }); }
      await onSaved();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Falha ao salvar.'); }
    finally { setBusy(false); }
  };
  const downloadTemplate = async () => {
    setBusy(true); setError('');
    try { await downloadExcel(`${getApiBaseUrl()}/products/skus/metadata/template`, 'modelo-cadastro-skus.xlsx', headers()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Falha ao baixar modelo.'); }
    finally { setBusy(false); }
  };
  return <div className="extraction-panel sku-metadata-editor" role="tabpanel">
    <div className="sku-metadata-fields">
      <label className="settings-field"><span>SKU</span><StyledSelect ariaLabel="SKU do cadastro" placeholder="Selecionar SKU" value={sku} disabled={busy || !items.length} onChange={selectSku} options={items.map((item) => ({ value: item.sku, label: item.sku }))} /></label>
      {(['category', 'cluster', 'subfamily'] as const).map((key) => <label className="settings-field" key={key}><span>{{ category: 'Categoria', cluster: 'Cluster', subfamily: 'Subfamília' }[key]}</span><input maxLength={250} disabled={busy || !sku} value={fields[key]} onChange={(event) => setFields((current) => ({ ...current, [key]: event.target.value }))} /></label>)}
      <label className="settings-field"><span>Mercado</span><StyledSelect ariaLabel="Mercado do SKU" value={fields.market_type} disabled={busy || !sku} onChange={(value) => setFields((current) => ({ ...current, market_type: value }))} options={[{ value: '', label: 'Não informado' }, { value: 'Cativo', label: 'Cativo' }, { value: 'Competitivo', label: 'Competitivo' }]} /></label>
    </div>
    {error && <p className="auth-error" role="alert">{error}</p>}{message && <p className="analytics-sheet__status" role="status">{message}</p>}
    {footerContainer && createPortal(<div className="sku-metadata-actions"><label className={`button-secondary sku-metadata-upload ${busy ? 'sku-metadata-upload--disabled' : ''}`}><Upload size={15} />Importar planilha<input type="file" accept=".xlsx,.xlsm,.csv" disabled={busy} onChange={async (event) => { const input = event.currentTarget; const file = input.files?.[0]; if (file) await save(file); input.value = ''; }} /></label><button type="button" className="button-secondary" disabled={busy} onClick={() => void downloadTemplate()}><FileDown size={15} />Baixar modelo</button><button type="button" className="button-primary" disabled={busy || !sku} onClick={() => void save()}>{busy ? <LoaderCircle size={15} className="animate-spin" /> : <CheckCircle2 size={15} />}Salvar cadastro</button></div>, footerContainer)}
  </div>;
}

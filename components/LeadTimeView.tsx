import DialogBackdrop from "./DialogBackdrop";
import OrbitingSpheres from "./OrbitingSpheres";
import SpreadsheetInput from "./SpreadsheetInput";
import ClearIcon from "./ClearIcon";
import { LOCAL_DATA_CHANGED } from "../src/localData";
import { apiAuthHeaders } from "../src/apiAuth";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { CalendarClock, ChevronRight, FileSpreadsheet, MapPin, RefreshCw, X } from "lucide-react";
import { StyledSelect } from "./StyledSelect";
import { useReportSku } from "../src/useReportSku";
import { formatDeliveryRange as days } from "../src/deliveryRange";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { useCurrencyFormatter } from "../src/useCurrencyFormatter";
import { downloadExcel } from "../src/download";

type Destination = { key?: string; uf: string; state: string; capital: string; postal_code: string; region: string; custom?: boolean };
type Cell = { status: string; delivery_min_days?: number | null; delivery_max_days?: number | null;
  delivery_days_type?: string | null; shipping_cost?: number | null; seller?: string; url?: string;
  captured_at?: string | null; stale?: boolean; error?: string; pending?: number; quoted_sellers?: number; alternatives?: Cell[] };
type Seller = { name: string; cells: Record<string, Cell> };
type Store = { key: string; name: string; offer_count: number; sellers: Seller[]; cells: Record<string, Cell> };
type Matrix = { sku: string; destinations: Destination[]; stores: Store[]; has_offers: boolean;
  progress: { total: number; completed: number; pending: number } };

const cep = (value: string) => `${value.slice(0, 5)}-${value.slice(5)}`;
const updated = (value?: string | null) => value ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).toLocaleString("pt-BR") : "Não informado";

const StoreLeadRows = memo(function StoreLeadRows({ store, destinations, money }: {
  store: Store; destinations: Destination[]; money: Intl.NumberFormat;
}) {
  const [open, setExpanded] = useState(false);
  const renderCell = (cell: Cell | undefined, dest: Destination, totalSellers?: number) => {
    const item = cell || { status: "not_queried" };
    const alternatives = item.alternatives || [];
    const details = alternatives.filter((option) => option.status === "quoted").map((option) =>
      `${option.seller}: ${days(option)}; frete ${option.shipping_cost == null ? "não cotado" : money.format(option.shipping_cost)}; ${updated(option.captured_at)}`).join("\n");
    const title = [`${dest.capital} · ${dest.uf} · CEP ${cep(dest.postal_code)}`, details || item.error || (item.status === "quoted" ? updated(item.captured_at) : "Prazo ainda não confirmado para este destino.")].join("\n");
    if (item.status === "quoted") return <div className={`lead-time-cell ${item.stale ? "lead-time-cell--stale" : ""}`} data-tooltip={title}>
      <strong>{days(item)}</strong><small>{item.shipping_cost == null ? "Frete não cotado" : money.format(item.shipping_cost)}</small>
      {totalSellers != null && <small>{item.quoted_sellers || 0}/{totalSellers} lojistas cotados</small>}
      {item.stale && <small>Mais de 24h</small>}{(item.pending || 0) > 0 && <small>Atualizando lojistas…</small>}
    </div>;
    if (item.status === "mixed") return <span className="lead-time-cell" data-tooltip={title}><strong>Prazos distintos</strong><small>Expanda os lojistas</small></span>;
    const labels: Record<string, string> = { no_offers: "Sem ofertas", out_of_stock: "Sem estoque", not_quoted: "Não informado", invalid_postal_code: "CEP não localizado", failed: "Falha na consulta", cancelled: "Cancelado", queued: "Na fila", running: "Consultando" };
    return <span className={`lead-time-cell lead-time-cell--${item.status}`} data-tooltip={title}>
      {item.status === "running" && <OrbitingSpheres size={13} className="animate-spin" />}{labels[item.status] || "Não consultado"}
    </span>;
  };
  return <>
    <tr>
      <th scope="row"><button type="button" onClick={() => setExpanded((value) => !value)} aria-expanded={open} disabled={!store.sellers.length}>
        <ChevronRight className="lead-time-chevron" size={16} aria-hidden="true" />
        <span>{store.name}<small>{store.sellers.length} lojistas · {store.offer_count} ofertas</small></span>
      </button></th>
      {destinations.map((dest) => <td key={dest.key || dest.uf}>{renderCell(store.cells[dest.key || dest.uf], dest, store.sellers.length)}</td>)}
    </tr>
    {open && store.sellers.map((seller) => <tr key={seller.name} className="lead-time-seller">
      <th scope="row">{seller.name}</th>
      {destinations.map((dest) => <td key={dest.key || dest.uf}>{renderCell(seller.cells[dest.key || dest.uf], dest)}</td>)}
    </tr>)}
  </>;
});

export default function LeadTimeView({ query, selectedSkus, canRefresh, headers }: {
  query: string; selectedSkus: string[]; canRefresh: boolean; headers: (initial?: HeadersInit) => Headers;
}) {
  const { candidates, sku, setSku, lookupSku } = useReportSku(query, selectedSkus);
  const [result, setResult] = useState<{ requestedSku: string; matrix: Matrix } | null>(null);
  const matrix = result?.requestedSku === lookupSku ? result.matrix : null;
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(()=>{const update=()=>setRevision(value=>value+1);window.addEventListener(LOCAL_DATA_CHANGED,update);return()=>window.removeEventListener(LOCAL_DATA_CHANGED,update);},[]);
  const [region, setRegion] = useState("");
  const [destinationsOpen, setDestinationsOpen] = useState(false);
  const [postalText, setPostalText] = useState("");
  const [postalSaving, setPostalSaving] = useState(false);
  const [postalLoading, setPostalLoading] = useState(false);
  const postalLoader = useRef<AbortController | null>(null);
  useEffect(() => () => {postalLoader.current?.abort();postalLoader.current=null;},[]);
  const [postalError, setPostalError] = useState("");
  const [exporting, setExporting] = useState(false);
  const money = useCurrencyFormatter();

  useEffect(() => {
    if (!lookupSku) { setResult(null); setLoading(false); setError(""); setMessage(""); return; }
    const controller = new AbortController();
    let timer: number | undefined;
    setLoading(true); setError(""); setMessage("");
    const load = async () => {
      try {
        const response = await fetch(`${getApiBaseUrl()}/products/lead-time/${encodeURIComponent(lookupSku)}`, { signal: controller.signal, headers: apiAuthHeaders() });
        if (!response.ok) throw new Error("Não foi possível carregar os prazos.");
        const data = await response.json() as Matrix;
        if (controller.signal.aborted) return;
        setResult({ requestedSku: lookupSku, matrix: data });
        if (data.progress.pending > 0) timer = window.setTimeout(() => void load(), 3000);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Falha ao carregar.");
      } finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load();
    return () => { controller.abort(); if (timer) window.clearTimeout(timer); };
  }, [lookupSku, revision]);

  const destinations = useMemo(() => matrix?.destinations.filter((dest) => !region || dest.region === region) || [], [matrix, region]);
  const pending = matrix?.progress.pending || 0;
  const consult = async (cancel = false) => {
    if (!lookupSku || sending || (!cancel && !destinations.length)) return;
    setSending(true); setError(""); setMessage("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/lead-time/${encodeURIComponent(sku)}/${cancel ? "cancel" : "refresh"}`, {
        method: "POST", headers: headers({ "Content-Type": "application/json" }),
        body: cancel ? undefined : JSON.stringify({ states: [...new Set(destinations.map((dest) => dest.uf))], postal_codes: destinations.map((dest) => dest.postal_code), force_refresh: true }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Não foi possível iniciar a consulta.");
      setMessage(cancel ? "Consulta cancelada." : data.message);
      setRevision((value) => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Falha na consulta."); }
    finally { setSending(false); }
  };

  const openDestinations = async () => {
    postalLoader.current?.abort();
    const controller=new AbortController();postalLoader.current=controller;
    const timer=window.setTimeout(()=>controller.abort(),15000);
    setDestinationsOpen(true); setPostalError(""); setPostalLoading(true);
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/lead-time/destinations`, { headers: apiAuthHeaders(), signal:controller.signal });
      if (!response.ok) throw new Error("Não foi possível carregar os CEPs. Verifique se o backend está atualizado.");
      const data = await response.json() as { destinations: Destination[] };
      if(controller.signal.aborted || postalLoader.current!==controller)return;
      setPostalText(data.destinations.map((dest) => cep(dest.postal_code)).join("\n"));
    } catch (reason) { if(postalLoader.current===controller)setPostalError(controller.signal.aborted ? "Tempo esgotado ao carregar CEPs. Você pode preencher a lista manualmente." : reason instanceof Error ? reason.message : "Falha ao carregar CEPs."); }
    finally { window.clearTimeout(timer);if(postalLoader.current===controller){postalLoader.current=null;setPostalLoading(false);} }
  };

  const saveDestinations = async (reset = false) => {
    const values = reset ? [] : [...new Set(postalText.split(/[\s,;]+/).filter(Boolean))];
    if (!reset && !values.length) { setPostalError("Informe pelo menos um CEP."); return; }
    setPostalSaving(true); setPostalError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/lead-time/destinations`, {
        method: "PUT", headers: headers({ "Content-Type": "application/json" }), body: JSON.stringify({ postal_codes: values }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Não foi possível salvar os CEPs.");
      setDestinationsOpen(false); setRevision((value) => value + 1);
    } catch (reason) { setPostalError(reason instanceof Error ? reason.message : "Falha ao salvar CEPs."); }
    finally { setPostalSaving(false); }
  };

  const exportQuotes = async () => {
    setExporting(true); setError("");
    try { await downloadExcel(`${getApiBaseUrl()}/products/lead-time/${encodeURIComponent(sku)}/export?${new URLSearchParams(region ? { region } : {})}`, `lead-time-${sku}.xlsx`, apiAuthHeaders()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Falha ao exportar."); }
    finally { setExporting(false); }
  };

  return <div className="sliding-tabs sliding-tabs--fixed mx-auto w-full max-w-[1600px] lead-time-view">
    <div className="workspace-toolbar lead-time-toolbar">
      <div className="lead-time-toolbar__filters">
        {candidates.length > 1 && <StyledSelect ariaLabel="SKU para Lead Time" value={sku} onChange={setSku} options={candidates.map((value) => ({ value, label: value }))} />}
        <StyledSelect ariaLabel="Filtrar região" value={region} onChange={setRegion} showAllOptions options={[{ value: "", label: "Brasil" }, ...["Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul"].map((value) => ({ value, label: value }))]} />
        {canRefresh && <button type="button" className="icon-only-button icon-button" aria-label="Definir CEPs" data-tooltip="Definir CEPs" onClick={() => void openDestinations()}><MapPin size={17} /></button>}
      </div>
      <div className="lead-time-toolbar__actions">
      <button type="button" aria-label="Atualizar" data-tooltip="Atualizar" className="icon-only-button icon-button" onClick={() => setRevision((value) => value + 1)} disabled={!lookupSku || loading}><RefreshCw size={15} /></button>
      <button type="button" aria-label="Exportar para Excel" data-tooltip="Exportar para Excel" className="icon-only-button icon-button" disabled={!matrix || loading || exporting} onClick={() => void exportQuotes()}>{exporting ? <OrbitingSpheres size={15} className="animate-spin" /> : <FileSpreadsheet size={15} />}</button>
      <button type="button" className="icon-only-button icon-button" aria-label="Consultar prazos" onClick={() => void consult()} disabled={!canRefresh || !matrix?.has_offers || !destinations.length || loading || sending || pending > 0} data-tooltip={!canRefresh ? "Entre com uma conta autorizada para consultar novas cotações" : "Consultar prazos"}>{sending ? <OrbitingSpheres size={17} /> : <CalendarClock size={17} />}</button>
      {pending > 0 && canRefresh && <button type="button" className="toolbar-button" disabled={sending} onClick={() => void consult(true)}><X size={15} />Cancelar</button>}
      </div>
    </div>
    {error && <p role="alert" className="analytics-sheet__error">{error}</p>}
    {message && <p role="status" className="analytics-sheet__status">{message}</p>}
    {pending > 0 && matrix && <div className="lead-time-progress" role="status"><OrbitingSpheres size={14} className="animate-spin" /><span>{matrix.progress.completed} de {matrix.progress.total} cotações concluídas</span><progress max={matrix.progress.total} value={matrix.progress.completed} /></div>}
    {!sku ? <div className="data-panel lead-time-empty"><CalendarClock size={28} /><h2>Lead Time por destino</h2><p>Escolha um SKU na barra de pesquisa superior para comparar lojas, estados e capitais.</p></div> : (loading || !lookupSku) && !matrix ? <div className="data-panel lead-time-empty" role="status" aria-label="Carregando prazos"><OrbitingSpheres className="animate-spin" size={24} aria-hidden="true" /></div> : matrix && <section className="data-panel lead-time-panel" aria-label={`Prazos de entrega de ${sku}`}>
      {!matrix.has_offers && <p className="analytics-sheet__status">Nenhuma oferta salva para este SKU. Faça uma extração para localizar os lojistas antes de consultar os prazos.</p>}
      <div className="lead-time-scroll" role="region" aria-label="Tabela de prazos por destino" tabIndex={0}><table className="lead-time-table"><thead><tr><th>Loja / Lojista</th>{destinations.map((dest) => <th key={dest.key || dest.uf} data-tooltip={`${dest.state} · ${dest.capital} · ${cep(dest.postal_code)}`}><strong>{dest.uf}</strong><small>{dest.capital}</small><small>{cep(dest.postal_code)}</small></th>)}</tr></thead><tbody>
        {matrix.stores.map((store) => <StoreLeadRows key={store.key} store={store} destinations={destinations} money={money} />)}</tbody></table></div>
    </section>}
    {destinationsOpen && <DialogBackdrop aria-label="Definir CEPs de consulta" onDismiss={() => !postalSaving && setDestinationsOpen(false)}>
      <section className="workspace-dialog destination-dialog">
        <div className="dialog-header"><div><h2>CEPs de consulta</h2></div><button type="button" className="icon-only-button icon-button" disabled={postalSaving} onClick={() => setDestinationsOpen(false)} aria-label="Fechar"><X size={17} /></button></div>
        <div className="destination-dialog__body"><SpreadsheetInput label="CEPs" kind="postal" value={postalText} onChange={setPostalText} disabled={postalSaving || postalLoading} action={<button type="button" className="icon-only-button icon-button" aria-label="Limpar CEPs" data-tooltip="Limpar CEPs" disabled={postalSaving || postalLoading} onClick={() => {setPostalText("");setPostalError("");}}>{postalLoading ? <OrbitingSpheres size={16}/> : <ClearIcon />}</button>}/>{postalError && <p role="alert" className="auth-error">{postalError}</p>}</div>
        <div className="modal-action-footer"><button type="button" className="button-secondary" disabled={postalSaving || postalLoading} onClick={() => void saveDestinations(true)}>Restaurar capitais</button><button type="button" className="button-primary" disabled={postalSaving || postalLoading} onClick={() => void saveDestinations()}>{postalSaving && <OrbitingSpheres size={15} className="animate-spin" />}Salvar CEPs</button></div>
      </section>
    </DialogBackdrop>}
  </div>;
}

import { apiAuthHeaders } from "../src/apiAuth";
import { useEffect, useMemo, useState } from "react";
import { CalendarClock, ChevronDown, ChevronRight, FileSpreadsheet, LoaderCircle, MapPin, RefreshCw, X } from "lucide-react";
import { StyledSelect } from "./StyledSelect";
import { navbarSkus } from "../src/skuSelection";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { createCurrencyFormatter, getInterfacePreferences } from "../src/preferences";
import { downloadExcel } from "../src/download";

type Destination = { key?: string; uf: string; state: string; capital: string; postal_code: string; region: string; custom?: boolean };
type Cell = { status: string; delivery_min_days?: number | null; delivery_max_days?: number | null;
  delivery_days_type?: string | null; shipping_cost?: number | null; seller?: string; url?: string;
  captured_at?: string | null; stale?: boolean; error?: string; pending?: number; quoted_sellers?: number; alternatives?: Cell[] };
type Seller = { name: string; cells: Record<string, Cell> };
type Store = { key: string; name: string; offer_count: number; sellers: Seller[]; cells: Record<string, Cell> };
type Matrix = { sku: string; destinations: Destination[]; stores: Store[]; has_offers: boolean;
  progress: { total: number; completed: number; pending: number } };

function days(cell: Cell) {
  const min = cell.delivery_min_days, max = cell.delivery_max_days;
  if (max == null) return "Não informado";
  const unit = cell.delivery_days_type === "business" ? "dias úteis" : cell.delivery_days_type === "calendar" ? "dias corridos" : "dias (tipo não informado)";
  return `${min == null ? `Até ${max}` : min !== max ? `${min}–${max}` : max} ${unit}`;
}
const cep = (value: string) => `${value.slice(0, 5)}-${value.slice(5)}`;
const updated = (value?: string | null) => value ? new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).toLocaleString("pt-BR") : "Não informado";

export default function LeadTimeView({ query, selectedSkus, canRefresh, headers }: {
  query: string; selectedSkus: string[]; canRefresh: boolean; headers: (initial?: HeadersInit) => Headers;
}) {
  const candidates = useMemo(() => navbarSkus(query, selectedSkus), [query, selectedSkus]);
  const [sku, setSku] = useState("");
  const [matrix, setMatrix] = useState<Matrix | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [revision, setRevision] = useState(0);
  const [region, setRegion] = useState("");
  const [expanded, setExpanded] = useState<string[]>([]);
  const [destinationsOpen, setDestinationsOpen] = useState(false);
  const [postalText, setPostalText] = useState("");
  const [postalSaving, setPostalSaving] = useState(false);
  const [postalError, setPostalError] = useState("");
  const [exporting, setExporting] = useState(false);
  const money = createCurrencyFormatter(getInterfacePreferences());

  useEffect(() => {
    const timer = window.setTimeout(() => setSku((current) => candidates.includes(current) ? current : candidates[0] || ""), 400);
    return () => window.clearTimeout(timer);
  }, [candidates]);

  useEffect(() => {
    if (!sku) { setMatrix(null); return; }
    const controller = new AbortController();
    let timer: number | undefined;
    setLoading(true); setError(""); setMessage(""); setMatrix(null);
    const load = async () => {
      try {
        const response = await fetch(`${getApiBaseUrl()}/products/lead-time/${encodeURIComponent(sku)}`, { signal: controller.signal, headers: apiAuthHeaders() });
        if (!response.ok) throw new Error("Não foi possível carregar os prazos.");
        const data = await response.json() as Matrix;
        if (controller.signal.aborted) return;
        setMatrix(data);
        if (data.progress.pending > 0) timer = window.setTimeout(() => void load(), 3000);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Falha ao carregar.");
      } finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load();
    return () => { controller.abort(); if (timer) window.clearTimeout(timer); };
  }, [sku, revision]);

  const destinations = matrix?.destinations.filter((dest) => !region || dest.region === region) || [];
  const pending = matrix?.progress.pending || 0;
  const consult = async (cancel = false) => {
    if (!sku || sending) return;
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
    setDestinationsOpen(true); setPostalError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/lead-time/destinations`, { headers: apiAuthHeaders() });
      if (!response.ok) throw new Error("Não foi possível carregar os CEPs. Verifique se o backend está atualizado.");
      const data = await response.json() as { destinations: Destination[] };
      setPostalText(data.destinations.map((dest) => cep(dest.postal_code)).join("\n"));
    } catch (reason) { setPostalError(reason instanceof Error ? reason.message : "Falha ao carregar CEPs."); }
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

  const renderCell = (cell: Cell | undefined, dest: Destination, totalSellers?: number) => {
    const item = cell || { status: "not_queried" };
    const alternatives = item.alternatives || [];
    const details = alternatives.filter((option) => option.status === "quoted").map((option) =>
      `${option.seller}: ${days(option)}; frete ${option.shipping_cost == null ? "não cotado" : money.format(option.shipping_cost)}; ${updated(option.captured_at)}`).join("\n");
    const title = [`${dest.capital} · ${dest.uf} · CEP ${cep(dest.postal_code)}`, details || item.error || "Prazo ainda não confirmado para este destino."].join("\n");
    if (item.status === "quoted") return <div className={`lead-time-cell ${item.stale ? "lead-time-cell--stale" : ""}`} title={title}>
      <strong>{days(item)}</strong><small>{item.shipping_cost == null ? "Frete não cotado" : money.format(item.shipping_cost)}</small>
      {totalSellers != null && <small>{item.quoted_sellers || 0}/{totalSellers} lojistas cotados</small>}
      {item.stale && <small>Mais de 24h</small>}{(item.pending || 0) > 0 && <small>Atualizando lojistas…</small>}
    </div>;
    if (item.status === "mixed") return <span className="lead-time-cell" title={title}><strong>Prazos distintos</strong><small>Expanda os lojistas</small></span>;
    const labels: Record<string, string> = { no_offers: "Sem ofertas", out_of_stock: "Sem estoque", not_quoted: "Não informado", failed: "Falha na consulta", cancelled: "Cancelado", queued: "Na fila", running: "Consultando" };
    return <span className={`lead-time-cell lead-time-cell--${item.status}`} title={title}>
      {item.status === "running" && <LoaderCircle size={13} className="animate-spin" />}{labels[item.status] || "Não consultado"}
    </span>;
  };

  return <div className="sliding-tabs sliding-tabs--fixed mx-auto w-full max-w-[1600px] lead-time-view">
    <div className="workspace-toolbar lead-time-toolbar">
      <div className="lead-time-toolbar__filters">
        {candidates.length > 1 && <StyledSelect ariaLabel="SKU para Lead Time" value={sku} onChange={setSku} options={candidates.map((value) => ({ value, label: value }))} />}
        <StyledSelect ariaLabel="Filtrar região" value={region} onChange={setRegion} options={[{ value: "", label: "Brasil" }, ...["Norte", "Nordeste", "Centro-Oeste", "Sudeste", "Sul"].map((value) => ({ value, label: value }))]} />
        {canRefresh && <button type="button" className="toolbar-button" onClick={() => void openDestinations()}><MapPin size={15} />Definir CEPs</button>}
      </div>
      <div className="lead-time-toolbar__actions">
      <button type="button" className="toolbar-button" onClick={() => setRevision((value) => value + 1)} disabled={!sku || loading}><RefreshCw size={15} />Atualizar</button>
      <button type="button" className="toolbar-button" disabled={!matrix || loading || exporting} onClick={() => void exportQuotes()}>{exporting ? <LoaderCircle size={15} className="animate-spin" /> : <FileSpreadsheet size={15} />}Exportar para Excel</button>
      <button type="button" className="toolbar-button" onClick={() => void consult()} disabled={!canRefresh || !matrix?.has_offers || sending || pending > 0} title={!canRefresh ? "Entre com uma conta autorizada para consultar novas cotações" : "Consulta os destinos exibidos para todas as lojas com ofertas salvas"}>{sending ? <LoaderCircle size={15} className="animate-spin" /> : <CalendarClock size={15} />}Consultar prazos</button>
      {pending > 0 && canRefresh && <button type="button" className="toolbar-button" disabled={sending} onClick={() => void consult(true)}><X size={15} />Cancelar</button>}
      </div>
    </div>
    {error && <p role="alert" className="analytics-sheet__error">{error}</p>}
    {message && <p role="status" className="analytics-sheet__status">{message}</p>}
    {pending > 0 && matrix && <div className="lead-time-progress" role="status"><LoaderCircle size={14} className="animate-spin" /><span>{matrix.progress.completed} de {matrix.progress.total} cotações concluídas</span><progress max={matrix.progress.total} value={matrix.progress.completed} /></div>}
    {!sku ? <div className="data-panel lead-time-empty"><CalendarClock size={28} /><h2>Lead Time por destino</h2><p>Escolha um SKU na barra de pesquisa superior para comparar lojas, estados e capitais.</p></div> : loading && !matrix ? <div className="data-panel lead-time-empty" role="status" aria-label="Carregando prazos"><LoaderCircle className="animate-spin" size={24} aria-hidden="true" /></div> : matrix && <section className="data-panel lead-time-panel" aria-label={`Prazos de entrega de ${sku}`}>
      {!matrix.has_offers && <p className="analytics-sheet__status">Nenhuma oferta salva para este SKU. Faça uma extração para localizar os lojistas antes de consultar os prazos.</p>}
      <div className="lead-time-scroll"><table className="lead-time-table"><thead><tr><th>Loja / Lojista</th>{destinations.map((dest) => <th key={dest.key || dest.uf} title={`${dest.state} · ${dest.capital} · ${cep(dest.postal_code)}`}><strong>{dest.uf}</strong><small>{dest.capital}</small><small>{cep(dest.postal_code)}</small></th>)}</tr></thead><tbody>
        {matrix.stores.flatMap((store) => {
          const open = expanded.includes(store.key);
          return [<tr key={store.key}><th><button type="button" onClick={() => setExpanded((current) => open ? current.filter((key) => key !== store.key) : [...current, store.key])} aria-expanded={open} disabled={!store.sellers.length}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<span>{store.name}<small>{store.sellers.length} lojistas · {store.offer_count} ofertas</small></span></button></th>{destinations.map((dest) => <td key={dest.key || dest.uf}>{renderCell(store.cells[dest.key || dest.uf], dest, store.sellers.length)}</td>)}</tr>,
            ...(open ? store.sellers.map((seller) => <tr key={`${store.key}:${seller.name}`} className="lead-time-seller"><th>{seller.name}</th>{destinations.map((dest) => <td key={dest.key || dest.uf}>{renderCell(seller.cells[dest.key || dest.uf], dest)}</td>)}</tr>) : [])];
        })}
      </tbody></table></div>
    </section>}
    {destinationsOpen && <div className="dialog-backdrop" onMouseDown={() => !postalSaving && setDestinationsOpen(false)}>
      <section className="workspace-dialog destination-dialog" role="dialog" aria-modal="true" aria-label="Definir CEPs de consulta" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-header"><div><span className="dialog-icon"><MapPin size={19} /></span><h2>CEPs de consulta</h2></div><button type="button" className="icon-button" disabled={postalSaving} onClick={() => setDestinationsOpen(false)} aria-label="Fechar"><X size={17} /></button></div>
        <div className="destination-dialog__body"><label className="textarea-field"><textarea value={postalText} onChange={(event) => setPostalText(event.target.value)} aria-label="CEPs em massa" /></label>{postalError && <p role="alert" className="auth-error">{postalError}</p>}</div>
        <div className="modal-action-footer"><button type="button" className="button-secondary" disabled={postalSaving} onClick={() => void saveDestinations(true)}>Restaurar capitais</button><button type="button" className="button-primary" disabled={postalSaving} onClick={() => void saveDestinations()}>{postalSaving && <LoaderCircle size={15} className="animate-spin" />}Salvar CEPs</button></div>
      </section>
    </div>}
  </div>;
}

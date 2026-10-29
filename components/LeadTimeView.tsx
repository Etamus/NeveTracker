import { useEffect, useMemo, useState } from "react";
import { CalendarClock, ChevronDown, ChevronRight, LoaderCircle, RefreshCw, X } from "lucide-react";
import { StyledSelect } from "./StyledSelect";
import { navbarSkus } from "../src/skuSelection";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { createCurrencyFormatter, getInterfacePreferences } from "../src/preferences";

type Destination = { uf: string; state: string; capital: string; postal_code: string; region: string };
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
        const response = await fetch(`${getApiBaseUrl()}/products/lead-time/${encodeURIComponent(sku)}`, { signal: controller.signal });
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
        body: cancel ? undefined : JSON.stringify({ states: destinations.map((dest) => dest.uf), force_refresh: true }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Não foi possível iniciar a consulta.");
      setMessage(cancel ? "Consulta cancelada." : data.message);
      setRevision((value) => value + 1);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Falha na consulta."); }
    finally { setSending(false); }
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
      </div>
      <div className="lead-time-toolbar__actions">
      <button type="button" className="toolbar-button" onClick={() => setRevision((value) => value + 1)} disabled={!sku || loading}><RefreshCw size={15} />Atualizar</button>
      <button type="button" className="toolbar-button" onClick={() => void consult()} disabled={!canRefresh || !matrix?.has_offers || sending || pending > 0} title={!canRefresh ? "Entre com uma conta autorizada para consultar novas cotações" : "Consulta os destinos exibidos para todas as lojas com ofertas salvas"}>{sending ? <LoaderCircle size={15} className="animate-spin" /> : <CalendarClock size={15} />}Consultar prazos</button>
      {pending > 0 && canRefresh && <button type="button" className="toolbar-button" disabled={sending} onClick={() => void consult(true)}><X size={15} />Cancelar</button>}
      </div>
    </div>
    {error && <p role="alert" className="analytics-sheet__error">{error}</p>}
    {message && <p role="status" className="analytics-sheet__status">{message}</p>}
    {pending > 0 && matrix && <div className="lead-time-progress" role="status"><LoaderCircle size={14} className="animate-spin" /><span>{matrix.progress.completed} de {matrix.progress.total} cotações concluídas</span><progress max={matrix.progress.total} value={matrix.progress.completed} /></div>}
    {!sku ? <div className="data-panel lead-time-empty"><CalendarClock size={28} /><h2>Lead Time por destino</h2><p>Escolha um SKU na barra de pesquisa superior para comparar lojas, estados e capitais.</p></div> : loading && !matrix ? <div className="data-panel lead-time-empty" role="status" aria-label="Carregando prazos"><LoaderCircle className="animate-spin" size={24} aria-hidden="true" /></div> : matrix && <section className="data-panel lead-time-panel" aria-label={`Prazos de entrega de ${sku}`}>
      {!matrix.has_offers && <p className="analytics-sheet__status">Nenhuma oferta salva para este SKU. Faça uma extração para localizar os lojistas antes de consultar os prazos.</p>}
      <div className="lead-time-scroll"><table className="lead-time-table"><thead><tr><th>Loja / Lojista</th>{destinations.map((dest) => <th key={dest.uf} title={`${dest.state} · ${dest.capital} · ${cep(dest.postal_code)}`}><strong>{dest.uf}</strong><small>{dest.capital}</small><small>{cep(dest.postal_code)}</small></th>)}</tr></thead><tbody>
        {matrix.stores.flatMap((store) => {
          const open = expanded.includes(store.key);
          return [<tr key={store.key}><th><button type="button" onClick={() => setExpanded((current) => open ? current.filter((key) => key !== store.key) : [...current, store.key])} aria-expanded={open} disabled={!store.sellers.length}>{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}<span>{store.name}<small>{store.sellers.length} lojistas · {store.offer_count} ofertas</small></span></button></th>{destinations.map((dest) => <td key={dest.uf}>{renderCell(store.cells[dest.uf], dest, store.sellers.length)}</td>)}</tr>,
            ...(open ? store.sellers.map((seller) => <tr key={`${store.key}:${seller.name}`} className="lead-time-seller"><th>{seller.name}</th>{destinations.map((dest) => <td key={dest.uf}>{renderCell(seller.cells[dest.uf], dest)}</td>)}</tr>) : [])];
        })}
      </tbody></table></div>
    </section>}
  </div>;
}

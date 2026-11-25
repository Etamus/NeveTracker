import OrbitingSpheres from "./OrbitingSpheres";
import { formatDeliveryRange } from "../src/deliveryRange";
import DatePeriodSelect from "./DatePeriodSelect";
import { LOCAL_DATA_CHANGED } from "../src/localData";
import { apiAuthHeaders } from "../src/apiAuth";
import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { FileSpreadsheet, RefreshCw } from "lucide-react";
import type { HistoryPoint } from "./HistoryPriceChart";
import { StyledSelect } from "./StyledSelect";
import PaginationFooter from "./PaginationFooter";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { navbarSkus } from "../src/skuSelection";
import { STORE_OPTIONS } from "../src/preferences";
import { useCurrencyFormatter } from "../src/useCurrencyFormatter";
import { downloadExcel } from "../src/download";

const HistoryPriceChart = lazy(() => import("./HistoryPriceChart"));

type StockEvent = { kind: "rupture" | "restock"; observed_at: string; previous_observed_at: string };
type HistoryRow = { date: string; sku: string; name: string; brand: string; source: string; seller: string; price: number | null;
  previous_price: number | null; difference: number | null; difference_percent: number | null; availability: string | null;
  observed_at: string; carried: boolean; checks: number; changed: boolean; postal_code: string | null;
  shipping_cost: number | null; delivery_min_days: number | null; delivery_max_days: number | null; delivery_days_type: string | null; changed_fields: string[]; stock_events?: StockEvent[] };
type History = { items: HistoryRow[]; total: number; first_observation: string | null;
  series: HistoryPoint[] };
type Group = { label: string; offers: number; available: number; skus: number; sellers: number; minimum: number | null; average: number | null; last_update: string | null };
type Comparison = { groups: Group[]; common_skus: number; items: { sku: string; left: number | null; right: number | null; difference: number | null }[] };
const fieldLabels: Record<string, string> = { price: "Preço", cash_price: "Preço à vista", installment_price: "Preço parcelado", installment_count: "Parcelas", installment_value: "Valor da parcela", shipping_cost: "Frete", shipping_method: "Serviço de entrega", delivery_min_days: "Prazo mínimo", delivery_max_days: "Prazo máximo", delivery_days_type: "Tipo de prazo", destination_zip: "CEP", availability: "Estoque", fulfillment_type: "Entrega", fulfilled_by: "Entregue por", name: "Nome", brand: "Marca" };
const localDate = () => new Intl.DateTimeFormat("en-CA", { timeZone: "America/Sao_Paulo", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const dateLabel = (value: string) => value.split("-").reverse().join("/");
const stamp = (value: string | null) => value ? new Date(/Z|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).toLocaleString("pt-BR") : "—";

export default function AnalysisReports({ mode, query, selectedSkus, sources, brands, search = "", dateFrom, dateTo, actionsContainer }: {
  mode: "comparison" | "history"; query: string; selectedSkus: string[]; sources: string[]; brands: string[];
  search?: string; dateFrom?: string; dateTo?: string; actionsContainer?: HTMLElement | null;
}) {
  const skus = useMemo(() => navbarSkus(query, selectedSkus), [query, selectedSkus]);
  const searchText = search.trim();
  const [appliedSearch, setAppliedSearch] = useState(searchText);
  useEffect(() => {
    if (searchText === appliedSearch) return;
    if (!searchText) { setAppliedSearch(""); return; }
    const timer = window.setTimeout(() => setAppliedSearch(searchText), 250);
    return () => window.clearTimeout(timer);
  }, [searchText, appliedSearch]);
  const [left, setLeft] = useState("Brastemp");
  const [right, setRight] = useState("Electrolux");
  const [options, setOptions] = useState<{ sources: string[]; brands: string[] }>({ sources: [], brands: [] });
  const [internalStart, setStart] = useState(() => { const date = new Date(`${localDate()}T12:00:00Z`); date.setUTCDate(date.getUTCDate()-29); return date.toISOString().slice(0, 10); });
  const [internalEnd, setEnd] = useState(localDate);
  const start=dateFrom ?? internalStart, end=dateTo ?? internalEnd;
  const [history, setHistory] = useState<History | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");
  const [exportError, setExportError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(()=>{const update=()=>setRevision(value=>value+1);window.addEventListener(LOCAL_DATA_CHANGED,update);return()=>window.removeEventListener(LOCAL_DATA_CHANGED,update);},[]);
  const [pagination, setPagination] = useState({ scope: "", page: 1 });
  const money = useCurrencyFormatter();
  const currency = (value: number | null | undefined) => value == null ? "Não informado" : money.format(value);

  useEffect(() => {
    if (mode !== "comparison") return;
    const controller = new AbortController();
    fetch(`${getApiBaseUrl()}/analysis/options`, { signal: controller.signal, headers: apiAuthHeaders() }).then((response) => {
      if (!response.ok) throw new Error("Não foi possível carregar as opções de comparação.");
      return response.json();
    }).then(setOptions).catch(() => undefined);
    return () => controller.abort();
  }, [mode, revision]);

  const reportUrl = useMemo(() => {
    const params = new URLSearchParams({ skus: skus.join(",") });
    if (appliedSearch) params.set("search", appliedSearch);
    sources.forEach((source) => params.append("source", source));
    brands.forEach((brand) => params.append("brand", brand));
    if (mode === "history") {
      if(start) params.set("start", start); if(end) params.set("end", end);
      if(!start && !end) params.set("all_dates", "true");
      params.set("carry", "true");
    } else {
      params.set("dimension", "source"); params.set("left", left); params.set("right", right);
      if(dateFrom)params.set("start",dateFrom);
      if(dateTo)params.set("end",dateTo);
    }
    return `${getApiBaseUrl()}/analysis/${mode}?${params}`;
  }, [mode, skus, sources, brands, appliedSearch, start, end, left, right, dateFrom, dateTo]);

  const page = pagination.scope === reportUrl ? pagination.page : 1;
  const url = mode === "history" ? `${reportUrl}&page=${page}` : reportUrl;
  useEffect(() => {
    setPagination((current) => current.scope === reportUrl ? current : { scope: reportUrl, page: 1 });
  }, [reportUrl]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setExportError("");
    if (mode === "comparison" && left === right) { setComparison(null); setLoading(false); return; }
    async function loadReport() {
      try {
        const response = await fetch(url, { signal: controller.signal, headers: apiAuthHeaders() });
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Não foi possível carregar a análise. Verifique se o backend está atualizado.");
        if (controller.signal.aborted) return;
        if (mode === "history") {
          const lastPage = Math.max(1, Math.ceil(result.total / 50));
          if (page > lastPage) setPagination({ scope: reportUrl, page: lastPage });
          else setHistory(result as History);
        } else setComparison(result as Comparison);
      } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Falha ao carregar."); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }
    void loadReport();
    return () => controller.abort();
  }, [url, mode, revision, page, reportUrl, left, right]);

  const exportReport = async () => {
    setExporting(true); setExportError("");
    try { await downloadExcel(`${url}&export=true`, mode === "history" ? "historico-precos.xlsx" : "comparacao-lojas.xlsx", apiAuthHeaders()); }
    catch (reason) { setExportError(reason instanceof Error ? reason.message : "Falha ao exportar."); }
    finally { setExporting(false); }
  };
  const choices = [...new Set([...STORE_OPTIONS.map((store) => store.label), ...options.sources])].map((value) => ({ value, label: value }));
  const comparisonGroups = comparison?.groups.filter((group) => group.offers > 0) || [];

  const actions = <div className="analysis-report__actions"><button type="button" aria-label="Atualizar" data-tooltip="Atualizar" className="icon-only-button icon-button" onClick={() => setRevision((value) => value+1)} disabled={loading}><RefreshCw size={15} /></button><button type="button" aria-label="Exportar para Excel" data-tooltip="Exportar para Excel" className="icon-only-button icon-button" onClick={() => void exportReport()} disabled={searchText !== appliedSearch || loading || exporting || !!error || (mode === "comparison" && left === right)}>{exporting ? <OrbitingSpheres size={15} className="animate-spin" /> : <FileSpreadsheet size={15} />}</button></div>;
  return <div className={mode === "history" ? "analysis-report analysis-report--history" : "analysis-report"}>
    {actionsContainer ? createPortal(actions,actionsContainer) : <div className="analysis-report__toolbar">
      {mode === "history" && dateFrom === undefined && <DatePeriodSelect from={start} to={end} onChange={(from, to) => { setStart(from); setEnd(to); }} allowAll maxDays={366} ariaLabel="Período do histórico" />}

      {actions}
    </div>}
    {(exportError || error) && <p role="alert" className="analytics-sheet__error">{exportError || (error === "Escolha um período de até 366 dias, com data final após a inicial." ? "O período informado é inválido." : error)}</p>}
    {mode === "comparison" && <div className="analysis-comparison-cards">{[{value: left, change: setLeft, name: "Loja A"}, {value: right, change: setRight, name: "Loja B"}].map((selection) => {
      const group = comparisonGroups.find((item) => item.label === selection.value);
      return <div key={selection.name} className="analysis-comparison-column"><StyledSelect ariaLabel={selection.name} value={selection.value} onChange={selection.change} options={choices} />{!loading && !error && group && <section className="data-panel analysis-comparison-card"><h2 className="metric-card__label">{group.label}</h2><dl><div><dt>Menor preço</dt><dd>{currency(group.minimum)}</dd></div><div><dt>Preço médio</dt><dd>{currency(group.average)}</dd></div></dl></section>}</div>;
    })}</div>}
    {loading ? <div className="freight-map__empty" role="status" aria-label="Carregando análise"><OrbitingSpheres size={24} className="animate-spin" /></div> : !error && (mode === "history" ? <>
      <div className="analysis-history-list">
      <section className="data-panel"><div className="data-panel__header"><div><h2>Evolução dos preços</h2></div></div>
        <div className="analysis-report__chart">{history?.series.some((item) => item.average != null) ? <Suspense fallback={<div className="freight-map__empty" role="status" aria-label="Carregando gráfico"><OrbitingSpheres size={24} /></div>}><HistoryPriceChart series={history.series} money={money} /></Suspense> : <div className="freight-map__empty">Nenhuma observação de preço no período selecionado.</div>}</div>
      </section>
      <section className="data-panel"><div className="data-panel__header"><div><h2>Histórico por oferta</h2></div></div><div className="analytics-sheet__scroll"><table className="analytics-sheet__table"><thead><tr>{["Data", "SKU", "Marca", "Loja", "Lojista", "Preço", "Dia anterior", "Diferença", "Variação", "Frete", "Prazo", "CEP", "Coletas", "Situação", "Alterações", "Observado em"].map((label) => <th key={label}>{label}</th>)}</tr></thead><tbody>{history?.items.map((row, index) => <tr key={`${row.date}-${row.sku}-${index}`}><td>{dateLabel(row.date)}</td><td>{row.sku}</td><td>{row.brand || "—"}</td><td>{row.source}</td><td>{row.seller}</td><td>{row.availability && /outofstock|out_of_stock|soldout|sold_out/i.test(row.availability) ? "Sem estoque" : currency(row.price)}</td><td>{currency(row.previous_price)}</td><td>{currency(row.difference)}</td><td>{row.difference_percent == null ? "—" : `${row.difference_percent.toFixed(1)}%`}</td><td>{currency(row.shipping_cost)}</td><td>{formatDeliveryRange(row)}</td><td>{row.postal_code || "Sem CEP na coleta"}</td><td>{row.checks}</td><td>{row.stock_events?.length ? <ul className="history-stock-events">{row.stock_events.map((event, i) => <li key={i}><strong data-event={event.kind}>{event.kind === "rupture" ? "Ruptura" : "Reposição"}</strong><small>{stamp(event.observed_at)} · Anterior: {stamp(event.previous_observed_at)}</small></li>)}</ul> : row.carried ? "Último valor conhecido" : row.changed ? "Alterado" : "Sem alteração"}</td><td data-tooltip={row.changed_fields.map((key) => fieldLabels[key] || key).join(", ")}>{row.changed_fields.length ? row.changed_fields.map((key) => fieldLabels[key] || key).join(", ") : "—"}</td><td>{stamp(row.observed_at)}</td></tr>)}{!history?.items.length && <tr><td colSpan={16} className="analytics-sheet__empty">Nenhum dado para os filtros selecionados.</td></tr>}</tbody></table></div></section></div><PaginationFooter page={page} pageSize={50} total={history?.total || 0} onPageChange={(next) => setPagination({ scope: reportUrl, page: next })} />
    </> : <>
      <section className="data-panel"><div className="data-panel__header"><div><h2>Comparação por SKU</h2></div></div><div className="analytics-sheet__scroll"><table className="analytics-sheet__table"><thead><tr><th>SKU</th><th>{left}</th><th>{right}</th><th>Diferença</th></tr></thead><tbody>{comparison?.items.map((row) => <tr key={row.sku}><td>{row.sku}</td><td>{currency(row.left)}</td><td>{currency(row.right)}</td><td>{currency(row.difference)}</td></tr>)}{!comparison?.items.length && <tr><td colSpan={4} className="analytics-sheet__empty">Nenhuma oferta para a comparação selecionada.</td></tr>}</tbody></table></div></section>
    </>)}
  </div>;
}

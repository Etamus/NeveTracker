import { apiAuthHeaders } from "../src/apiAuth";
import { useEffect, useMemo, useState } from "react";
import { FileSpreadsheet, LoaderCircle, RefreshCw } from "lucide-react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { StyledSelect } from "./StyledSelect";
import PaginationFooter from "./PaginationFooter";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { navbarSkus } from "../src/skuSelection";
import { createCurrencyFormatter, getInterfacePreferences, STORE_OPTIONS } from "../src/preferences";
import { downloadExcel } from "../src/download";

type HistoryRow = { date: string; sku: string; name: string; brand: string; source: string; seller: string; price: number | null;
  previous_price: number | null; difference: number | null; difference_percent: number | null; availability: string | null;
  observed_at: string; carried: boolean; checks: number; changed: boolean; postal_code: string | null;
  shipping_cost: number | null; delivery_max_days: number | null; delivery_days_type: string | null; changed_fields: string[] };
type History = { items: HistoryRow[]; total: number; first_observation: string | null;
  series: { date: string; minimum: number | null; average: number | null; maximum: number | null; offers: number }[] };
type Group = { label: string; offers: number; available: number; skus: number; sellers: number; minimum: number | null; average: number | null; last_update: string | null };
type Comparison = { groups: Group[]; common_skus: number; items: { sku: string; left: number | null; right: number | null; difference: number | null }[] };
const fieldLabels: Record<string, string> = { price: "Preço", cash_price: "Preço à vista", installment_price: "Preço parcelado", installment_count: "Parcelas", installment_value: "Valor da parcela", shipping_cost: "Frete", shipping_method: "Serviço de entrega", delivery_min_days: "Prazo mínimo", delivery_max_days: "Prazo máximo", delivery_days_type: "Tipo de prazo", destination_zip: "CEP", availability: "Estoque", fulfillment_type: "Entrega", fulfilled_by: "Entregue por", name: "Nome", brand: "Marca" };
const localDate = () => new Intl.DateTimeFormat("en-CA", { timeZone: "America/Sao_Paulo", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const dateLabel = (value: string) => value.split("-").reverse().join("/");
const stamp = (value: string | null) => value ? new Date(/Z|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).toLocaleString("pt-BR") : "—";

export default function AnalysisReports({ mode, query, selectedSkus, sources, brands }: {
  mode: "comparison" | "history"; query: string; selectedSkus: string[]; sources: string[]; brands: string[];
}) {
  const skus = useMemo(() => navbarSkus(query, selectedSkus), [query, selectedSkus]);
  const [left, setLeft] = useState("Brastemp");
  const [right, setRight] = useState("Electrolux");
  const [options, setOptions] = useState<{ sources: string[]; brands: string[] }>({ sources: [], brands: [] });
  const [start, setStart] = useState(() => { const date = new Date(`${localDate()}T12:00:00Z`); date.setUTCDate(date.getUTCDate()-29); return date.toISOString().slice(0, 10); });
  const [end, setEnd] = useState(localDate);
  const [history, setHistory] = useState<History | null>(null);
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [loading, setLoading] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState(1);
  const money = createCurrencyFormatter(getInterfacePreferences());
  const currency = (value: number | null | undefined) => value == null ? "—" : money.format(value);

  useEffect(() => {
    const controller = new AbortController();
    fetch(`${getApiBaseUrl()}/analysis/options`, { signal: controller.signal, headers: apiAuthHeaders() }).then((response) => {
      if (!response.ok) throw new Error("Não foi possível carregar as opções de comparação.");
      return response.json();
    }).then(setOptions).catch(() => undefined);
    return () => controller.abort();
  }, [revision]);

  const url = useMemo(() => {
    const params = new URLSearchParams({ skus: skus.join(","), page: String(page) });
    sources.forEach((source) => params.append("source", source));
    brands.forEach((brand) => params.append("brand", brand));
    if (mode === "history") {
      params.set("start", start); params.set("end", end); params.set("carry", "true");
    } else { params.set("dimension", "source"); params.set("left", left); params.set("right", right); }
    return `${getApiBaseUrl()}/analysis/${mode}?${params}`;
  }, [mode, skus, page, sources, brands, start, end, left, right]);

  useEffect(() => { setPage(1); }, [mode, skus, sources, brands, start, end]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    if (mode === "comparison" && left === right) { setComparison(null); setLoading(false); return; }
    const timer = window.setTimeout(async () => {
      try {
        const response = await fetch(url, { signal: controller.signal, headers: apiAuthHeaders() });
        const result = await response.json();
        if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Não foi possível carregar a análise. Verifique se o backend está atualizado.");
        if (controller.signal.aborted) return;
        if (mode === "history") setHistory(result as History); else setComparison(result as Comparison);
      } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Falha ao carregar."); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }, 300);
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [url, mode, revision]);

  const exportReport = async () => {
    setExporting(true); setError("");
    try { await downloadExcel(`${url}&export=true`, mode === "history" ? "historico-precos.xlsx" : "comparacao-lojas.xlsx", apiAuthHeaders()); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "Falha ao exportar."); }
    finally { setExporting(false); }
  };
  const choices = [...new Set([...STORE_OPTIONS.map((store) => store.label), ...options.sources])].map((value) => ({ value, label: value }));

  return <div className="analysis-report">
    <div className="analysis-report__toolbar">
      {mode === "history" ? <>
        <label>De<input type="date" value={start} onChange={(event) => setStart(event.target.value)} aria-label="Data inicial do histórico" /></label>
        <label>Até<input type="date" value={end} onChange={(event) => setEnd(event.target.value)} aria-label="Data final do histórico" /></label>
      </> : <>
        <StyledSelect ariaLabel="Grupo A" value={left} onChange={setLeft} options={choices} /><span className="analysis-report__versus">vs</span><StyledSelect ariaLabel="Grupo B" value={right} onChange={setRight} options={choices} />
      </>}
      <div className="analysis-report__actions"><button type="button" className="toolbar-button" onClick={() => setRevision((value) => value+1)} disabled={loading}><RefreshCw size={15} />Atualizar</button><button type="button" className="toolbar-button" onClick={() => void exportReport()} disabled={loading || exporting || !!error || (mode === "comparison" && left === right)}>{exporting ? <LoaderCircle size={15} className="animate-spin" /> : <FileSpreadsheet size={15} />}Exportar para Excel</button></div>
    </div>
    {error && !(mode === "history" && error === "Escolha um período de até 366 dias, com data final após a inicial.") && <p role="alert" className="analytics-sheet__error">{error}</p>}
    {loading ? <div className="freight-map__empty" role="status" aria-label="Carregando análise"><LoaderCircle size={24} className="animate-spin" /></div> : !error && (mode === "history" ? <>
      <section className="data-panel"><div className="data-panel__header"><div><h2>Evolução dos preços</h2><p>Valores observados e último valor conhecido.</p></div></div>
        <div className="analysis-report__chart">{history?.series.some((item) => item.average != null) ? <ResponsiveContainer width="100%" height={280}><LineChart data={history.series}><CartesianGrid stroke="var(--border)" strokeDasharray="3 3" /><XAxis dataKey="date" tickFormatter={dateLabel} tick={{ fill: "var(--muted)", fontSize: 10 }} /><YAxis tick={{ fill: "var(--muted)", fontSize: 10 }} /><Tooltip labelFormatter={(label) => dateLabel(String(label))} formatter={(value) => currency(Number(value))} contentStyle={{ background: "var(--surface)", borderColor: "var(--border)", borderRadius: 8 }} /><Legend /><Line dataKey="minimum" name="Mínimo" stroke="var(--success)" dot={false} /><Line dataKey="average" name="Média" stroke="var(--accent)" strokeWidth={2} dot={false} /><Line dataKey="maximum" name="Máximo" stroke="var(--muted)" dot={false} /></LineChart></ResponsiveContainer> : <div className="freight-map__empty">Nenhuma observação de preço no período selecionado.</div>}</div>
      </section>
      <section className="data-panel"><div className="data-panel__header"><div><h2>Histórico por oferta</h2></div></div><div className="analytics-sheet__scroll"><table className="analytics-sheet__table"><thead><tr>{["Data", "SKU", "Marca", "Loja", "Lojista", "Preço", "Dia anterior", "Diferença", "Variação", "Frete", "Prazo", "CEP", "Coletas", "Situação", "Alterações", "Observado em"].map((label) => <th key={label}>{label}</th>)}</tr></thead><tbody>{history?.items.map((row, index) => <tr key={`${row.date}-${row.sku}-${index}`}><td>{dateLabel(row.date)}</td><td>{row.sku}</td><td>{row.brand || "—"}</td><td>{row.source}</td><td>{row.seller}</td><td>{row.availability && /outofstock|out_of_stock|soldout|sold_out/i.test(row.availability) ? "Sem estoque" : currency(row.price)}</td><td>{currency(row.previous_price)}</td><td>{currency(row.difference)}</td><td>{row.difference_percent == null ? "—" : `${row.difference_percent.toFixed(1)}%`}</td><td>{currency(row.shipping_cost)}</td><td>{row.delivery_max_days == null ? "—" : `Até ${row.delivery_max_days} ${row.delivery_days_type === "business" ? "dias úteis" : row.delivery_days_type === "calendar" ? "dias corridos" : "dias"}`}</td><td>{row.postal_code || "—"}</td><td>{row.checks}</td><td>{row.carried ? "Último valor conhecido" : row.changed ? "Alterado" : "Sem alteração"}</td><td title={row.changed_fields.map((key) => fieldLabels[key] || key).join(", ")}>{row.changed_fields.length ? row.changed_fields.map((key) => fieldLabels[key] || key).join(", ") : "—"}</td><td>{stamp(row.observed_at)}</td></tr>)}{!history?.items.length && <tr><td colSpan={16} className="analytics-sheet__empty">Nenhum dado para os filtros selecionados.</td></tr>}</tbody></table></div><PaginationFooter page={page} pageSize={50} total={history?.total || 0} onPageChange={setPage} /></section>
    </> : <>
      <div className="analysis-comparison-cards">{comparison?.groups.map((group) => <section key={group.label} className="data-panel analysis-comparison-card"><h2>{group.label}</h2><dl><div><dt>Menor preço</dt><dd>{currency(group.minimum)}</dd></div><div><dt>Preço médio</dt><dd>{currency(group.average)}</dd></div><div><dt>SKUs / Lojistas</dt><dd>{group.skus} / {group.sellers}</dd></div><div><dt>Ofertas disponíveis</dt><dd>{group.available} de {group.offers}</dd></div></dl><small>Última observação: {stamp(group.last_update)}</small></section>)}</div>
      <section className="data-panel"><div className="data-panel__header"><div><h2>Comparação por SKU</h2></div></div><div className="analytics-sheet__scroll"><table className="analytics-sheet__table"><thead><tr><th>SKU</th><th>{left}</th><th>{right}</th><th>Diferença B − A</th></tr></thead><tbody>{comparison?.items.map((row) => <tr key={row.sku}><td>{row.sku}</td><td>{currency(row.left)}</td><td>{currency(row.right)}</td><td>{currency(row.difference)}</td></tr>)}{!comparison?.items.length && <tr><td colSpan={4} className="analytics-sheet__empty">Nenhuma oferta para a comparação selecionada.</td></tr>}</tbody></table></div></section>
    </>)}
  </div>;
}

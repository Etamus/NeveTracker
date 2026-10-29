import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, ExternalLink, FileSpreadsheet, Funnel, LoaderCircle, RefreshCw, Search, SquareCheckBig, X } from "lucide-react";
import type { Product, SkuStatusItem, SkuStatusResponse } from "../models/product.model";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import PaginationFooter from "./PaginationFooter";
import { offerIsAvailable } from "../src/offerAvailability";
import { createCurrencyFormatter, getInterfacePreferences, INTERFACE_PREFERENCES_EVENT } from "../src/preferences";

type SheetResponse = { items: Product[]; total: number; page: number; page_size: number };
type SummaryItem = {
  highest_price?: number | null;
  reference_price?: number | null;
  reference_difference?: number | null;
  internal_code?: string | null;
  external_id?: string | null;
  sku: string; name: string; subfamily?: string | null;
  sku_category?: string | null; cluster?: string | null; market_type?: string | null;
  store_count: number; seller_count: number; offer_count: number; pma: number | null;
  cheapest_price: number | null; cheapest_store: string; difference_value: number | null; difference_percent: number | null;
  availability?: string | null;
};
type SummaryResponse = { items: SummaryItem[]; total: number; page: number; page_size: number };
type SheetOptions = { sources: string[]; sellers: string[] };
const PAGE_SIZE = 50;

function formatUpdate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? "—" : new Intl.DateTimeFormat("pt-BR", {
    dateStyle: "short", timeStyle: "short",
  }).format(date);
}

function formatDays(min?: number | null, max?: number | null, type?: string | null) {
  if (min == null && max == null) return "—";
  const unit = type === "business" ? "dias úteis" : "dias";
  if (min == null) return `Até ${max} ${unit}`;
  if (max == null) return `A partir de ${min} ${unit}`;
  if (min != null && max != null && min !== max) return `${min}–${max} ${unit}`;
  return `${max ?? min} ${unit}`;
}

export default function AnalyticsSpreadsheet() {
  const [view, setView] = useState<"summary" | "offers">("summary");
  const [skus, setSkus] = useState("");
  const [search, setSearch] = useState("");
  const [appliedSkus, setAppliedSkus] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [sources, setSources] = useState<string[]>([]);
  const [sellers, setSellers] = useState<string[]>([]);
  const [options, setOptions] = useState<SheetOptions>({ sources: [], sellers: [] });
  const [offerResult, setOfferResult] = useState<SheetResponse>({ items: [], total: 0, page: 1, page_size: PAGE_SIZE });
  const [summaryResult, setSummaryResult] = useState<SummaryResponse>({ items: [], total: 0, page: 1, page_size: PAGE_SIZE });
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [filterOpen, setFilterOpen] = useState(false);
  const [sourceOpen, setSourceOpen] = useState(true);
  const [fulfillment, setFulfillment] = useState<string[]>([]);
  const [fulfillmentOpen, setFulfillmentOpen] = useState(false);
  const [sellerOpen, setSellerOpen] = useState(false);
  const [skuSelectorOpen, setSkuSelectorOpen] = useState(false);
  const [skuSelectorFilter, setSkuSelectorFilter] = useState("");
  const [skuSelectorItems, setSkuSelectorItems] = useState<SkuStatusItem[]>([]);
  const [skuSelectorLoading, setSkuSelectorLoading] = useState(false);
  const [skuSelectorError, setSkuSelectorError] = useState("");
  const [draftSkus, setDraftSkus] = useState<string[]>([]);
  const filterRef = useRef<HTMLDivElement>(null);
  const [, setPreferenceRevision] = useState(0);
  const currency = createCurrencyFormatter(getInterfacePreferences());

  useEffect(() => {
    const update = () => setPreferenceRevision((value) => value + 1);
    window.addEventListener(INTERFACE_PREFERENCES_EVENT, update);
    return () => window.removeEventListener(INTERFACE_PREFERENCES_EVENT, update);
  }, []);

  useEffect(() => {
    const closeOutside = (event: MouseEvent) => {
      if (filterRef.current && !filterRef.current.contains(event.target as Node)) setFilterOpen(false);
    };
    document.addEventListener("mousedown", closeOutside);
    return () => document.removeEventListener("mousedown", closeOutside);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setAppliedSkus(skus);
      setAppliedSearch(search);
      setPage(1);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [skus, search]);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams();
    sources.forEach((value) => params.append("source", value));
    fetch(`${getApiBaseUrl()}/products/analytics/sheet/options?${params}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Não foi possível carregar os filtros.");
        return response.json() as Promise<SheetOptions>;
      })
      .then((nextOptions) => {
        setOptions(nextOptions);
        setSellers((current) => {
          const retained = current.filter((value) => nextOptions.sellers.includes(value));
          return retained.length === current.length ? current : retained;
        });
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); });
    return () => controller.abort();
  }, [sources, revision]);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
    if (appliedSkus.trim()) params.set("skus", appliedSkus);
    if (appliedSearch.trim()) params.set("search", appliedSearch);
    sources.forEach((value) => params.append("source", value));
    sellers.forEach((value) => params.append("seller", value));
    fulfillment.forEach((value) => params.append("fulfillment", value));
    setLoading(true);
    setError("");
    if (view === "summary") setSummaryResult({ items: [], total: 0, page, page_size: PAGE_SIZE });
    else setOfferResult({ items: [], total: 0, page, page_size: PAGE_SIZE });
    const endpoint = view === "summary" ? "/products/analytics/sheet/summary" : "/products/analytics/sheet";
    fetch(`${getApiBaseUrl()}${endpoint}?${params}`, { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("Não foi possível carregar a planilha.");
        return response.json() as Promise<SheetResponse | SummaryResponse>;
      })
      .then((data) => {
        if (view === "summary") setSummaryResult(data as SummaryResponse);
        else setOfferResult(data as SheetResponse);
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(String(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [appliedSkus, appliedSearch, sources, sellers, fulfillment, page, revision, view]);

  const result = view === "summary" ? summaryResult : offerResult;
  const hasCodes = summaryResult.items.some((item) => item.internal_code || item.external_id);
  const filtersPending = skus !== appliedSkus || search !== appliedSearch;
  const cellMoney = (value?: number | null) => value == null ? "—" : currency.format(value);
  const visibleSkuSelectorItems = skuSelectorItems.filter((item) =>
    item.sku.toLocaleLowerCase("pt-BR").includes(skuSelectorFilter.trim().toLocaleLowerCase("pt-BR")),
  );
  const toggleSource = (value: string) => {
    setSources((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
    setPage(1);
  };
  const toggleSeller = (value: string) => {
    setSellers((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
    setPage(1);
  };

  const openSkuSelector = async () => {
    setDraftSkus([...new Set(skus.split(/[,;\s]+/).map((value) => value.trim().toUpperCase()).filter(Boolean))]);
    setSkuSelectorFilter("");
    setSkuSelectorError("");
    setSkuSelectorOpen(true);
    setSkuSelectorLoading(true);
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/skus/status`);
      if (!response.ok) throw new Error("Não foi possível carregar os SKUs.");
      const data = await response.json() as SkuStatusResponse;
      setSkuSelectorItems(data.items || []);
    } catch (reason) {
      setSkuSelectorError(reason instanceof Error ? reason.message : "Não foi possível carregar os SKUs.");
    } finally {
      setSkuSelectorLoading(false);
    }
  };

  const applySelectedSkus = () => {
    setSkus(draftSkus.join(", "));
    setPage(1);
    setSkuSelectorOpen(false);
  };

  const exportAnalytics = async () => {
    if (exporting || loading || filtersPending || !result.total) return;
    setExporting(true);
    setExportError("");
    try {
      const params = new URLSearchParams();
      if (skus.trim()) params.set("skus", skus);
      if (search.trim()) params.set("search", search);
      sources.forEach((value) => params.append("source", value));
      sellers.forEach((value) => params.append("seller", value));
    fulfillment.forEach((value) => params.append("fulfillment", value));
      const response = await fetch(`${getApiBaseUrl()}/products/analytics/sheet/export?${params}`);
      if (!response.ok) {
        const body = await response.json().catch(() => null);
        throw new Error(body?.detail || "Não foi possível exportar os dados.");
      }
      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") || "";
      const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "nevetracker-analytics.xlsx";
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setExportError(reason instanceof Error ? reason.message : "Não foi possível exportar os dados.");
    } finally {
      setExporting(false);
    }
  };

  return (
    <>
      <div className="workspace-toolbar analytics-sheet-toolbar">
        <div className="result-search analytics-sheet-toolbar__search">
          <button type="button" className={skus ? "sku-select-trigger sku-select-trigger--active" : "sku-select-trigger"} onClick={() => void openSkuSelector()} aria-label="Selecionar SKUs do banco local" title="Selecionar SKUs">
            <SquareCheckBig size={17} />
            {skus && <span>{skus.split(/[,;\s]+/).filter(Boolean).length}</span>}
          </button>
          <input value={skus} onChange={(event) => setSkus(event.target.value)} placeholder="Pesquisar materiais..." aria-label="Filtrar por SKUs" />
          {skus && <button type="button" onClick={() => setSkus("")} aria-label="Limpar SKUs"><X size={14} /></button>}
        </div>
        <div className="result-search analytics-sheet-toolbar__search">
          <Search size={16} />
          <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Buscar produto..." aria-label="Buscar produto" />
          {search && <button type="button" onClick={() => setSearch("")} aria-label="Limpar produto"><X size={14} /></button>}
        </div>
        <div ref={filterRef} className="relative ml-auto">
          <button type="button" className={`toolbar-button ${filterOpen ? "toolbar-button--active" : ""}`} onClick={() => setFilterOpen((open) => !open)} aria-expanded={filterOpen}>
            <Funnel size={15} /> Filtros {(sources.length + sellers.length + fulfillment.length > 0) && <span className="filter-count">{sources.length + sellers.length + fulfillment.length}</span>}
          </button>
          {filterOpen && <div className="filter-popover analytics-sheet__filter-popover">
            <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
              <p className="text-xs font-semibold text-[var(--foreground)]">Filtrar resultados</p>
              {(sources.length + sellers.length + fulfillment.length > 0) && <button type="button" className="text-xs font-semibold text-[var(--accent)]" onClick={() => { setSources([]); setSellers([]); setFulfillment([]); setPage(1); }}>Limpar</button>}
            </div>
            <div className="filter-sections">
              <div className="filter-section">
                <button type="button" className="filter-section__trigger" onClick={() => setFulfillmentOpen((open) => !open)} aria-expanded={fulfillmentOpen}><span>Entrega</span><span className="flex items-center gap-2">{fulfillment.length > 0 && <span className="filter-section__count">{fulfillment.length}</span>}<ChevronDown size={15} className={fulfillmentOpen ? "rotate-180" : ""} /></span></button>
                {fulfillmentOpen && <div className="filter-section__content">
                  {[{value: "2P", label: "2P · Plataforma"}, {value: "3P", label: "3P · Lojista"}, {value: "unknown", label: "Não informada"}].map(({value, label}) => <button type="button" key={value} className="filter-option" onClick={() => { setFulfillment((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]); setPage(1); }}><span className={`filter-checkbox ${fulfillment.includes(value) ? "filter-checkbox--active" : ""}`}>{fulfillment.includes(value) && <Check size={11} strokeWidth={3} />}</span><span>{label}</span></button>)}
                </div>}
              </div>
              <div className="filter-section">
                <button type="button" className="filter-section__trigger" onClick={() => setSourceOpen((open) => !open)}><span>Lojas</span><span className="flex items-center gap-2">{sources.length > 0 && <span className="filter-section__count">{sources.length}</span>}<ChevronDown size={15} className={sourceOpen ? "rotate-180" : ""} /></span></button>
                {sourceOpen && <div className="filter-section__content analytics-sheet__filter-list">
                  {options.sources.map((value) => <button type="button" key={value} className="filter-option" onClick={() => toggleSource(value)}><span className={`filter-checkbox ${sources.includes(value) ? "filter-checkbox--active" : ""}`}>{sources.includes(value) && <Check size={11} strokeWidth={3} />}</span><span className="truncate">{value}</span></button>)}
                </div>}
              </div>
              <div className="filter-section">
                <button type="button" className="filter-section__trigger" onClick={() => setSellerOpen((open) => !open)}><span>Lojistas</span><span className="flex items-center gap-2">{sellers.length > 0 && <span className="filter-section__count">{sellers.length}</span>}<ChevronDown size={15} className={sellerOpen ? "rotate-180" : ""} /></span></button>
                {sellerOpen && <div className="filter-section__content">
                  <div className="analytics-sheet__filter-list">
                    {options.sellers.map((value) => <button type="button" key={value} className="filter-option" onClick={() => toggleSeller(value)}><span className={`filter-checkbox ${sellers.includes(value) ? "filter-checkbox--active" : ""}`}>{sellers.includes(value) && <Check size={11} strokeWidth={3} />}</span><span className="truncate">{value}</span></button>)}
                  </div>
                </div>}
              </div>
            </div>
          </div>}
        </div>
        <button type="button" className="toolbar-button" onClick={() => setRevision((value) => value + 1)} aria-label="Atualizar Database"><RefreshCw size={15} /> Atualizar</button>
        <button type="button" className="icon-button analytics-export-button" onClick={() => void exportAnalytics()} disabled={exporting || loading || filtersPending || !result.total} aria-label="Exportar Database para Excel" title="Exportar para Excel">
          {exporting ? <LoaderCircle size={17} className="animate-spin" /> : <FileSpreadsheet size={17} />}
        </button>
      </div>
      <div className="tab-scroll-region">
      <section className="data-panel analytics-sheet" aria-label="Database de ofertas">
      <div className="analytics-sheet__views" role="tablist" aria-label="Tipo de planilha">
        <button type="button" role="tab" aria-selected={view === "summary"} className={view === "summary" ? "is-active" : ""} onClick={() => { setView("summary"); setPage(1); }}>Resumo por SKU</button>
        <button type="button" role="tab" aria-selected={view === "offers"} className={view === "offers" ? "is-active" : ""} onClick={() => { setView("offers"); setPage(1); }}>Resumo por Lojista</button>
      </div>
      {(error || exportError) && <p className="analytics-sheet__error" role="alert">{error || exportError}</p>}
      <div className={`analytics-sheet__scroll ${loading && !result.items.length ? "analytics-sheet__scroll--loading" : ""}`}>
        {view === "summary" ? (
        <table className="analytics-sheet__table">
          <thead><tr><th>SKU</th>{hasCodes && <><th>Código interno</th><th>ID externo</th></>}<th>Produto</th><th>Subfamília</th><th>Categoria</th><th>Cluster</th><th>Mercado</th><th>Lojas</th><th>Lojistas</th><th>Ofertas</th><th>Preço médio</th><th>Melhor preço</th><th>Maior preço</th><th>Melhor lojista</th><th title="Seu preço informado na importação de SKUs">Meu preço</th><th title="Meu preço menos o melhor preço encontrado">Meu preço − melhor preço</th><th title="Preço médio menos o melhor preço">Diferença</th><th>Diferença (%)</th></tr></thead>
          <tbody>
            {summaryResult.items.map((item) => <tr key={item.sku}>
              <td className="analytics-sheet__sku">{item.sku}</td>{hasCodes && <><td>{item.internal_code || "—"}</td><td>{item.external_id || "—"}</td></>}<td title={item.name}>{item.name}{item.offer_count === 0 ? <small> · Sem ofertas localizadas</small> : !offerIsAvailable(item) && <small className="text-[var(--danger)]"> · Sem estoque</small>}</td><td>{item.subfamily || "—"}</td><td>{item.sku_category || "—"}</td><td>{item.cluster || "—"}</td><td>{item.market_type || "—"}</td>
              <td className="analytics-sheet__number">{item.store_count}</td><td className="analytics-sheet__number">{item.seller_count}</td><td className="analytics-sheet__number">{item.offer_count}</td><td className="analytics-sheet__number">{cellMoney(item.pma)}</td><td className="analytics-sheet__number">{cellMoney(item.cheapest_price)}</td><td className="analytics-sheet__number">{cellMoney(item.highest_price)}</td><td>{item.cheapest_store || "—"}</td><td className="analytics-sheet__number">{cellMoney(item.reference_price)}</td><td className="analytics-sheet__number">{cellMoney(item.reference_difference)}</td><td className="analytics-sheet__number">{cellMoney(item.difference_value)}</td><td className="analytics-sheet__number">{item.difference_percent == null ? "—" : `${item.difference_percent.toFixed(1)}%`}</td>
            </tr>)}
          </tbody>
        </table>
        ) : (
        <table className="analytics-sheet__table">
          <thead><tr>
            <th>SKU</th><th>Produto</th><th>Subfamília</th><th>Categoria</th><th>Cluster</th><th>Mercado</th><th>Lojas</th><th>Lojistas</th><th>Preço</th><th>À vista</th><th>A prazo</th><th>Frete</th><th>Total + frete</th><th>Prazo</th><th>CEP</th><th title="2P: entrega pela plataforma; 3P: entrega pelo lojista">Logística 2P/3P</th><th>Entregue por</th><th>Atualizado em</th><th>Oferta</th>
          </tr></thead>
          <tbody>
            {offerResult.items.map((item) => <tr key={`${item.id}-${item.url}`}>
              <td className="analytics-sheet__sku">{item.sku || "—"}</td>
              <td title={item.name}>{item.name}{!offerIsAvailable(item) && <small className="text-[var(--danger)]"> · Sem estoque</small>}</td><td>{item.subfamily || "—"}</td><td>{item.sku_category || item.category || "—"}</td><td>{item.cluster || "—"}</td><td>{item.market_type || "—"}</td>
              <td>{item.source || item.store}</td><td className="analytics-sheet__seller">{item.store}</td>
              <td className="analytics-sheet__number">{offerIsAvailable(item) ? cellMoney(item.current_price) : "Sem estoque"}</td><td className="analytics-sheet__number">{cellMoney(item.cash_price)}</td><td className="analytics-sheet__number">{cellMoney(item.installment_price)}</td>
              <td className="analytics-sheet__number">{item.shipping_cost == null ? "Não cotado" : cellMoney(item.shipping_cost)}</td><td className="analytics-sheet__number">{item.shipping_cost == null || item.current_price == null ? "—" : cellMoney(item.current_price + item.shipping_cost)}</td>
              <td>{formatDays(item.delivery_min_days, item.delivery_max_days, item.delivery_days_type)}</td><td>{item.destination_zip || "—"}</td>
              <td title={item.fulfillment_evidence || "Responsabilidade da entrega não informada pela loja"}>{item.fulfillment_type === "2P" ? "2P · Plataforma" : item.fulfillment_type === "3P" ? "3P · Lojista" : "Não informado"}</td>
              <td>{item.fulfilled_by || "—"}</td><td>{formatUpdate(item.last_update)}</td>
              <td><a href={item.url} target="_blank" rel="noreferrer" aria-label={`Abrir oferta de ${item.store}`}><ExternalLink size={15} /></a></td>
            </tr>)}
          </tbody>
        </table>
        )}
        {loading && !result.items.length && <div className="analytics-sheet__loading" role="status" aria-label="Carregando dados"><LoaderCircle size={22} className="animate-spin" aria-hidden="true" /></div>}
      </div>
      <PaginationFooter page={page} pageSize={PAGE_SIZE} total={result.total} onPageChange={setPage} />
    </section>
    </div>
    {skuSelectorOpen && createPortal(
      <div className="dialog-backdrop" role="presentation" onMouseDown={() => setSkuSelectorOpen(false)}>
        <section className="workspace-dialog workspace-dialog--sku-selector" role="dialog" aria-modal="true" aria-label="Selecionar SKUs" onMouseDown={(event) => event.stopPropagation()}>
          <div className="dialog-header">
            <div><span className="dialog-icon"><SquareCheckBig size={19} /></span><h2>Selecionar SKUs</h2></div>
            <button type="button" className="icon-button" onClick={() => setSkuSelectorOpen(false)} aria-label="Fechar"><X size={17} /></button>
          </div>
          <div className="sku-selector-toolbar">
            <Search size={16} />
            <input type="search" value={skuSelectorFilter} onChange={(event) => setSkuSelectorFilter(event.target.value)} placeholder="Filtrar SKUs do banco..." aria-label="Filtrar SKUs" autoFocus />
          </div>
          <div className="sku-selector-list">
            {skuSelectorLoading && !skuSelectorItems.length ? <p className="sku-status-empty">Carregando...</p> : visibleSkuSelectorItems.length ? visibleSkuSelectorItems.map((item) => (
              <label key={item.sku} className="sku-selector-row">
                <input type="checkbox" checked={draftSkus.includes(item.sku)} onChange={() => setDraftSkus((current) => current.includes(item.sku) ? current.filter((value) => value !== item.sku) : [...current, item.sku])} />
                <span className="min-w-0 flex-1"><strong>{item.sku}</strong><small>{item.offer_count} ofertas · {item.store_count} lojas · {formatUpdate(item.last_update)}</small></span>
              </label>
            )) : <p className="sku-status-empty">Nenhum SKU encontrado.</p>}
          </div>
          {skuSelectorError && <p className="auth-error" role="alert">{skuSelectorError}</p>}
          <div className="sku-selector-footer">
            <span>{draftSkus.length} de {skuSelectorItems.length} selecionados</span>
            <div className="sku-selector-footer__actions">
              <button type="button" className="button-secondary" onClick={() => setDraftSkus((current) => current.length ? [] : visibleSkuSelectorItems.map((item) => item.sku))}>{draftSkus.length ? "Desmarcar tudo" : "Marcar tudo"}</button>
              <button type="button" className="button-primary" onClick={applySelectedSkus}>Aplicar</button>
            </div>
          </div>
        </section>
      </div>, document.body,
    )}
    </>
  );
}

import { useFilterExpansion } from "../src/useFilterExpansion";
import SkuPickerDialog from "./SkuPickerDialog";
import DatePeriodSelect from "./DatePeriodSelect";
import { formatDeliveryRange } from "../src/deliveryRange";
import OrbitingSpheres from "./OrbitingSpheres";
import { LOCAL_DATA_CHANGED } from "../src/localData";
import { apiAuthHeaders } from "../src/apiAuth";
import { downloadExcel } from "../src/download";
import { useEffect, useMemo, useRef, useState } from "react";
import { Box, Check, ChevronDown, ExternalLink, FileSpreadsheet, Funnel, RefreshCw, SquareCheckBig, Store, X } from "lucide-react";
import type { Product } from "../models/product.model";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import PaginationFooter from "./PaginationFooter";
import ViewTabs from "./ViewTabs";
import { StyledSelect } from "./StyledSelect";
import { offerIsAvailable } from "../src/offerAvailability";
import { useCurrencyFormatter } from "../src/useCurrencyFormatter";
import { useAnchoredPopover } from "./useAnchoredPopover";

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
const databaseViews = [
  { key: "summary", label: "Resumo por SKU", icon: Box },
  { key: "offers", label: "Resumo por Lojista", icon: Store },
] as const;
const updateFormatter = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });

function formatUpdate(value?: string | null) {
  if (!value) return "Não informado";
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? "Não informado" : updateFormatter.format(date);
}

export default function AnalyticsSpreadsheet({ clearOnEmpty = false }: { clearOnEmpty?: boolean }) {
  const [view, setView] = useState<"summary" | "offers">("summary");
  const [skus, setSkus] = useState("");
  const [appliedSkus, setAppliedSkus] = useState("");
  useEffect(() => { if (clearOnEmpty) { setSkus(""); setAppliedSkus(""); setPage(1); } }, [clearOnEmpty]);
  const [sources, setSources] = useState<string[]>([]);
  const [sellers, setSellers] = useState<string[]>([]);
  const [options, setOptions] = useState<SheetOptions>({ sources: [], sellers: [] });
  const [optionsError, setOptionsError] = useState("");
  const [offerResult, setOfferResult] = useState<SheetResponse>({ items: [], total: 0, page: 1, page_size: PAGE_SIZE });
  const [summaryResult, setSummaryResult] = useState<SummaryResponse>({ items: [], total: 0, page: 1, page_size: PAGE_SIZE });
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const [filterOpen, setFilterOpen] = useState(false);
  const [fulfillment, setFulfillment] = useState<string[]>([]);
  const [{ sourceOpen, fulfillmentOpen, sellerOpen, periodOpen }, setOpenGroups] = useFilterExpansion("database", {
    sourceOpen: false, fulfillmentOpen: false, sellerOpen: false, periodOpen: false,
  });
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [dateOrder, setDateOrder] = useState<"asc" | "desc">("desc");
  const [skuSelectorOpen, setSkuSelectorOpen] = useState(false);
  const filterRef = useRef<HTMLDivElement>(null);
  const filterButton = useRef<HTMLButtonElement>(null);
  const filterMenu = useRef<HTMLDivElement>(null);
  useAnchoredPopover(filterOpen, filterButton, filterMenu);
  const lastPageTotal = useRef({ scope: "", total: 0 });
  const currency = useCurrencyFormatter();
  const invalidPeriod = !!(dateFrom && dateTo && dateFrom > dateTo);
  const filterCount = sources.length + sellers.length + fulfillment.length + Number(!!(dateFrom || dateTo)) + Number(dateOrder === "asc");

  useEffect(()=>{const update=()=>setRevision(value=>value+1);window.addEventListener(LOCAL_DATA_CHANGED,update);return()=>window.removeEventListener(LOCAL_DATA_CHANGED,update);},[]);

  useEffect(() => {
    if (!filterOpen) return;
    const closeOutside = (event: MouseEvent) => {
      if (filterRef.current && !filterRef.current.contains(event.target as Node)) setFilterOpen(false);
    };
    document.addEventListener("mousedown", closeOutside);
    return () => document.removeEventListener("mousedown", closeOutside);
  }, [filterOpen]);

  useEffect(() => {
    if (skus === appliedSkus) return;
    if (!skus) { setAppliedSkus(""); setPage(1); return; }
    const timer = window.setTimeout(() => {
      setAppliedSkus(skus);
      setPage(1);
    }, 350);
    return () => window.clearTimeout(timer);
  }, [skus, appliedSkus]);

  useEffect(() => {
    const controller = new AbortController();
    if (!filterOpen) return;
    setOptionsError("");
    const params = new URLSearchParams();
    sources.forEach((value) => params.append("source", value));
    fetch(`${getApiBaseUrl()}/products/analytics/sheet/options?${params}`, { signal: controller.signal, headers: apiAuthHeaders() })
      .then((response) => {
        if (!response.ok) throw new Error("Não foi possível carregar os filtros.");
        return response.json() as Promise<SheetOptions>;
      })
      .then((nextOptions) => {
        if (controller.signal.aborted) return;
        setOptions(nextOptions);
        setSellers((current) => {
          const retained = current.filter((value) => nextOptions.sellers.includes(value));
          return retained.length === current.length ? current : retained;
        });
      })
      .catch(() => { if (!controller.signal.aborted) setOptionsError("Não foi possível carregar as lojas e os lojistas."); });
    return () => controller.abort();
  }, [filterOpen, sources, revision]);

  const filters = useMemo(() => {
    const params = new URLSearchParams();
    if (appliedSkus.trim()) {
      const codes=appliedSkus.trim().split(/[,;\s]+/);
      if(codes.length>1 && codes.every(code=>/\d/.test(code)))params.set("skus",codes.join(','));
      else params.set("search",appliedSkus);
    }
    sources.forEach((value) => params.append("source", value));
    sellers.forEach((value) => params.append("seller", value));
    fulfillment.forEach((value) => params.append("fulfillment", value));
    if (dateFrom) params.set("date_from", dateFrom);
    if (dateTo) params.set("date_to", dateTo);
    params.set("date_order", dateOrder);
    return params.toString();
  }, [appliedSkus, sources, sellers, fulfillment, dateFrom, dateTo, dateOrder]);

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams(filters);
    params.set("page", String(page));
    params.set("page_size", String(PAGE_SIZE));
    if (clearOnEmpty && !appliedSkus.trim()) {
      setOfferResult({ items: [], total: 0, page: 1, page_size: PAGE_SIZE });
      setSummaryResult({ items: [], total: 0, page: 1, page_size: PAGE_SIZE });
      setError(""); setLoading(false);
      return;
    }
    if (invalidPeriod) { setLoading(false); setError("A data final deve ser igual ou posterior à data inicial."); return; }
    setLoading(true);
    setError("");
    const scope = view + "?" + filters;
    const blank = { items: [], total: lastPageTotal.current.scope === scope ? lastPageTotal.current.total : 0, page, page_size: PAGE_SIZE };
    if (view === "summary") setSummaryResult(blank);
    else setOfferResult(blank);
    const endpoint = view === "summary" ? "/products/analytics/sheet/summary" : "/products/analytics/sheet";
    fetch(`${getApiBaseUrl()}${endpoint}?${params}`, { signal: controller.signal, headers: apiAuthHeaders() })
      .then((response) => {
        if (!response.ok) throw new Error("Não foi possível carregar a planilha.");
        return response.json() as Promise<SheetResponse | SummaryResponse>;
      })
      .then((data) => {
        if (controller.signal.aborted) return;
        lastPageTotal.current = { scope, total: data.total };
        const lastPage = Math.max(1, Math.ceil(data.total / PAGE_SIZE));
        if (page > lastPage) { setPage(lastPage); return; }
        if (view === "summary") setSummaryResult(data as SummaryResponse);
        else setOfferResult(data as SheetResponse);
      })
      .catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Não foi possível carregar a planilha."); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [appliedSkus, filters, invalidPeriod, page, revision, view, clearOnEmpty]);

  const result = view === "summary" ? summaryResult : offerResult;
  const hasCodes = summaryResult.items.some((item) => item.internal_code || item.external_id);
  const filtersPending = skus !== appliedSkus;
  const cellMoney = (value?: number | null) => value == null ? "Não informado" : currency.format(value);
  const toggleSource = (value: string) => {
    setSources((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
    setPage(1);
  };
  const toggleSeller = (value: string) => {
    setSellers((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]);
    setPage(1);
  };

  const applySelectedSkus = (items: string[]) => {
    const selected = items.join(", ");
    setSkus(selected);
    setAppliedSkus(selected);
    setPage(1);
    setSkuSelectorOpen(false);
  };

  const exportAnalytics = async () => {
    if (exporting || loading || error || filtersPending || invalidPeriod || !result.total ||
        lastPageTotal.current.scope !== `${view}?${filters}`) return;
    setExporting(true);
    setExportError("");
    try {
      await downloadExcel(`${getApiBaseUrl()}/products/analytics/sheet/export?${filters}`, undefined, apiAuthHeaders());
    } catch (reason) {
      setExportError(reason instanceof Error ? reason.message : "Não foi possível exportar os dados.");
    } finally {
      setExporting(false);
    }
  };

  return (
    <>
      <div className="workspace-toolbar analytics-sheet-toolbar">
        <div className="analytics-search-controls">
          <div className="result-search">
            <button type="button" className={skus ? "sku-select-trigger sku-select-trigger--active" : "sku-select-trigger"} onClick={() => setSkuSelectorOpen(true)} aria-label="Selecionar SKUs do banco local" data-tooltip="Selecionar SKUs">
              <SquareCheckBig size={17} />
              {skus && <span>{skus.split(/[,;\s]+/).filter(Boolean).length}</span>}
            </button>
            <input value={skus} onChange={(event) => setSkus(event.target.value)} placeholder="Buscar" aria-label="Filtrar por SKUs" />
            {skus && <button className="icon-only-button" type="button" onClick={() => setSkus("")} aria-label="Limpar SKUs"><X size={14} /></button>}
          </div>
          <DatePeriodSelect from={dateFrom} to={dateTo} ariaLabel="Período do Database" allowAll
            onChange={(from, to) => { setDateFrom(from); setDateTo(to); setPage(1); }} />
        </div>
        <div ref={filterRef} className="relative ml-auto" onKeyDown={(event) => {
          if (filterOpen && event.key === "Escape" && !event.defaultPrevented) {
            const innerMenu = filterRef.current?.querySelector('[aria-expanded="true"][aria-haspopup]');
            if (innerMenu) return;
            setFilterOpen(false);
            filterButton.current?.focus();
          }
        }}>
          <button ref={filterButton} type="button" className={`toolbar-button workspace-dropdown-trigger ${filterOpen ? "toolbar-button--active" : ""}`} onClick={() => setFilterOpen((open) => !open)} aria-expanded={filterOpen}>
            <Funnel size={15} /> Filtros {filterCount > 0 && <span className="filter-count">{filterCount}</span>}
          </button>
          {filterOpen && <div ref={filterMenu} popover="manual" className="filter-popover workspace-filter-popover">
            <div className="workspace-filter-popover__header flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
              <p className="text-xs font-semibold text-[var(--foreground)]">Filtrar resultados</p>
              {filterCount > 0 && <button type="button" className="text-xs font-semibold text-[var(--accent)]" onClick={() => { setSources([]); setSellers([]); setFulfillment([]); setDateFrom(""); setDateTo(""); setDateOrder("desc"); setPage(1); }}>Limpar</button>}
            </div>
            <div className="filter-sections">
              {optionsError && <p className="analytics-sheet__error" role="alert">{optionsError}</p>}
              <div className="filter-section">
                <button type="button" className="filter-section__trigger" onClick={() => setOpenGroups((groups) => ({ ...groups, periodOpen: !groups.periodOpen }))} aria-expanded={periodOpen}><span>Ordenar por data</span><ChevronDown size={15} className={periodOpen ? "rotate-180" : ""} /></button>
                {periodOpen && <div className="filter-section__content analytics-sheet__period">
                  <StyledSelect ariaLabel="Ordenar Database por data" value={dateOrder} onChange={(value) => { setDateOrder(value); setPage(1); }} options={[{ value: "desc", label: "Mais recentes" }, { value: "asc", label: "Mais antigas" }]} />
                </div>}
              </div>
              <div className="filter-section">
                <button type="button" className="filter-section__trigger" onClick={() => setOpenGroups((groups) => ({ ...groups, fulfillmentOpen: !groups.fulfillmentOpen }))} aria-expanded={fulfillmentOpen}><span>Entrega</span><span className="flex items-center gap-2">{fulfillment.length > 0 && <span className="filter-section__count">{fulfillment.length}</span>}<ChevronDown size={15} className={fulfillmentOpen ? "rotate-180" : ""} /></span></button>
                {fulfillmentOpen && <div className="filter-section__content">
                  {[{value: "2P", label: "2P · Plataforma"}, {value: "3P", label: "3P · Lojista"}, {value: "unknown", label: "Não informada"}].map(({value, label}) => <button type="button" key={value} className="filter-option" aria-pressed={fulfillment.includes(value)} onClick={() => { setFulfillment((current) => current.includes(value) ? current.filter((item) => item !== value) : [...current, value]); setPage(1); }}><span className={`filter-checkbox ${fulfillment.includes(value) ? "filter-checkbox--active" : ""}`}>{fulfillment.includes(value) && <Check size={11} strokeWidth={3} />}</span><span>{label}</span></button>)}
                </div>}
              </div>
              {options.sources.length > 0 && <div className="filter-section">
                <button type="button" className="filter-section__trigger" aria-expanded={sourceOpen} onClick={() => setOpenGroups((groups) => ({ ...groups, sourceOpen: !groups.sourceOpen }))}><span>Lojas</span><span className="flex items-center gap-2">{sources.length > 0 && <span className="filter-section__count">{sources.length}</span>}<ChevronDown size={15} className={sourceOpen ? "rotate-180" : ""} /></span></button>
                {sourceOpen && <div className="filter-section__content analytics-sheet__filter-list">
                  {options.sources.map((value) => <button type="button" key={value} className="filter-option" aria-pressed={sources.includes(value)} onClick={() => toggleSource(value)}><span className={`filter-checkbox ${sources.includes(value) ? "filter-checkbox--active" : ""}`}>{sources.includes(value) && <Check size={11} strokeWidth={3} />}</span><span className="truncate">{value}</span></button>)}
                </div>}
              </div>}
              {options.sellers.length > 0 && <div className="filter-section">
                <button type="button" className="filter-section__trigger" aria-expanded={sellerOpen} onClick={() => setOpenGroups((groups) => ({ ...groups, sellerOpen: !groups.sellerOpen }))}><span>Lojistas</span><span className="flex items-center gap-2">{sellers.length > 0 && <span className="filter-section__count">{sellers.length}</span>}<ChevronDown size={15} className={sellerOpen ? "rotate-180" : ""} /></span></button>
                {sellerOpen && <div className="filter-section__content">
                  <div className="analytics-sheet__filter-list">
                    {options.sellers.map((value) => <button type="button" key={value} className="filter-option" aria-pressed={sellers.includes(value)} onClick={() => toggleSeller(value)}><span className={`filter-checkbox ${sellers.includes(value) ? "filter-checkbox--active" : ""}`}>{sellers.includes(value) && <Check size={11} strokeWidth={3} />}</span><span className="truncate">{value}</span></button>)}
                  </div>
                </div>}
              </div>}
            </div>
          </div>}
        </div>
        <button type="button" className="icon-only-button toolbar-button" disabled={loading} onClick={() => setRevision((value) => value + 1)} aria-label="Atualizar Database" data-tooltip="Atualizar"><RefreshCw size={15} /></button>
        <button type="button" className="icon-only-button icon-button analytics-export-button" onClick={() => void exportAnalytics()} disabled={exporting || loading || !!error || filtersPending || invalidPeriod || !result.total} aria-label="Exportar Database para Excel" data-tooltip="Exportar para Excel">
          {exporting ? <OrbitingSpheres size={17} className="animate-spin" /> : <FileSpreadsheet size={17} />}
        </button>
      </div>
      <div className="tab-scroll-region paginated-region">
      <section className="data-panel analytics-sheet paginated-panel" aria-label="Database de ofertas">
      <ViewTabs className="analytics-sheet__views" label="Tipo de planilha" panelId="database-view-panel" tabs={databaseViews} value={view} onChange={(value) => { setView(value); setPage(1); }} />
      <div id="database-view-panel" role="tabpanel" className="paginated-panel__body" aria-labelledby={`database-view-panel-tab-${view}`}>
      {(error || exportError) && <p className="analytics-sheet__error" role="alert">{error || exportError}</p>}
      <div role="region" aria-label={view === "summary" ? "Tabela por SKU" : "Tabela por lojista"} aria-busy={loading} tabIndex={0} className={`analytics-sheet__scroll ${loading && !result.items.length ? "analytics-sheet__scroll--loading" : ""}`}>
        {view === "summary" ? (
        <table className="analytics-sheet__table">
          <thead><tr><th>SKU</th>{hasCodes && <><th>Código interno</th><th>ID externo</th></>}<th>Produto</th><th>Subfamília</th><th>Categoria</th><th>Cluster</th><th>Mercado</th><th>Lojas</th><th>Lojistas</th><th>Ofertas</th><th>Preço médio</th><th>Melhor preço</th><th>Maior preço</th><th>Melhor lojista</th><th data-tooltip="Preço médio menos o melhor preço">Diferença</th><th>Diferença (%)</th></tr></thead>
          <tbody>
            {summaryResult.items.map((item) => <tr key={item.sku}>
              <td className="analytics-sheet__sku">{item.sku}</td>{hasCodes && <><td>{item.internal_code || "Não informado"}</td><td>{item.external_id || "Não informado"}</td></>}<td data-tooltip={item.name}>{item.name}{item.offer_count === 0 ? <small> · Sem ofertas localizadas</small> : !offerIsAvailable(item) && <small className="text-[var(--danger)]"> · Sem estoque</small>}</td><td>{item.subfamily || "Não informado"}</td><td>{item.sku_category || "Não informado"}</td><td>{item.cluster || "Não informado"}</td><td>{item.market_type || "Não informado"}</td>
              <td className="analytics-sheet__number">{item.store_count}</td><td className="analytics-sheet__number">{item.seller_count}</td><td className="analytics-sheet__number">{item.offer_count}</td><td className="analytics-sheet__number">{cellMoney(item.pma)}</td><td className="analytics-sheet__number">{cellMoney(item.cheapest_price)}</td><td className="analytics-sheet__number">{cellMoney(item.highest_price)}</td><td>{item.cheapest_store || "Não informado"}</td><td className="analytics-sheet__number">{cellMoney(item.difference_value)}</td><td className="analytics-sheet__number">{item.difference_percent == null ? "Não informado" : `${item.difference_percent.toFixed(1)}%`}</td>
            </tr>)}
          </tbody>
        </table>
        ) : (
        <table className="analytics-sheet__table">
          <thead><tr>
            <th>SKU</th><th>Produto</th><th>Subfamília</th><th>Categoria</th><th>Cluster</th><th>Mercado</th><th>Lojas</th><th>Lojistas</th><th>Preço</th><th>À vista</th><th>A prazo</th><th>Frete</th><th>Total + frete</th><th>Prazo</th><th>CEP</th><th data-tooltip="2P: entrega pela plataforma; 3P: entrega pelo lojista">Logística 2P/3P</th><th>Entregue por</th><th>Atualizado em</th><th>Oferta</th>
          </tr></thead>
          <tbody>
            {offerResult.items.map((item) => <tr key={`${item.id}-${item.url}-${item.destination_zip || ''}`}>
              <td className="analytics-sheet__sku">{item.sku || "Não informado"}</td>
              <td data-tooltip={item.name}>{item.name}{!offerIsAvailable(item) && <small className="text-[var(--danger)]"> · Sem estoque</small>}</td><td>{item.subfamily || "Não informado"}</td><td>{item.sku_category || item.category || "Não informado"}</td><td>{item.cluster || "Não informado"}</td><td>{item.market_type || "Não informado"}</td>
              <td>{item.source || item.store}</td><td className="analytics-sheet__seller">{item.store}</td>
              <td className="analytics-sheet__number">{offerIsAvailable(item) ? cellMoney(item.current_price) : "Sem estoque"}</td><td className="analytics-sheet__number">{cellMoney(item.cash_price)}</td><td className="analytics-sheet__number">{cellMoney(item.installment_price)}</td>
              <td className="analytics-sheet__number">{item.shipping_cost == null ? "Não cotado" : cellMoney(item.shipping_cost)}</td><td className="analytics-sheet__number">{item.shipping_cost == null || item.current_price == null ? "Não informado" : cellMoney(item.current_price + item.shipping_cost)}</td>
              <td>{formatDeliveryRange(item, "Não informado")}</td><td>{item.destination_zip || "Sem CEP na coleta"}</td>
              <td data-tooltip={item.fulfillment_evidence || "Responsabilidade da entrega não informada pela loja"}>{item.fulfillment_type === "2P" ? "2P · Plataforma" : item.fulfillment_type === "3P" ? "3P · Lojista" : "Não informado"}</td>
              <td>{item.fulfilled_by || "Não informado"}</td><td>{formatUpdate(item.last_update)}</td>
              <td><a className="icon-only-button" href={item.url} target="_blank" rel="noreferrer" aria-label={`Abrir oferta de ${item.store}`} data-tooltip="Abrir oferta"><ExternalLink size={15} /></a></td>
            </tr>)}
          </tbody>
        </table>
        )}
        {loading && !result.items.length && <div className="analytics-sheet__loading" role="status" aria-label="Carregando dados"><OrbitingSpheres size={22} className="animate-spin" aria-hidden="true" /></div>}
      </div>
      {!loading && !error && !result.items.length && <p className="analytics-sheet__no-results" role="status">Nenhum dado para os filtros selecionados.</p>}
      <PaginationFooter page={page} pageSize={PAGE_SIZE} total={result.total} onPageChange={setPage} />
      </div>
    </section>
    </div>
    {skuSelectorOpen && (
      <SkuPickerDialog
        initialSelection={[...new Set(skus.split(/[,;\s]+/).map(value => value.trim().toUpperCase()).filter(Boolean))]}
        action="Aplicar"
        allowEmpty
        onApply={applySelectedSkus}
        onDismiss={() => setSkuSelectorOpen(false)}
      />
    )}
    </>
  );
}

import { useEffect, useMemo, useState } from "react";
import { ExternalLink, LoaderCircle, MapPin, RefreshCw } from "lucide-react";
import { StyledSelect } from "./StyledSelect";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { createCurrencyFormatter, getInterfacePreferences, INTERFACE_PREFERENCES_EVENT } from "../src/preferences";
import { navbarSkus } from "../src/skuSelection";
import shapes from "../src/data/brazilStates.json";
import { canonicalFreightStore, loadFreightMap, type FreightData } from "../src/freightMapData";

const formatCep = (value: string) => `${value.slice(0, 5)}-${value.slice(5)}`;
const formatDate = (value: string) => new Date(/Z|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`).toLocaleString("pt-BR");

export default function FreightMap({ query, selectedSkus, sources, canRefresh, headers }: {
  query: string; selectedSkus: string[]; sources: string[]; canRefresh: boolean; headers: (initial?: HeadersInit) => Headers;
}) {
  const candidates = useMemo(() => navbarSkus(query, selectedSkus), [query, selectedSkus]);
  const [sku, setSku] = useState("");
  const [data, setData] = useState<FreightData | null>(null);
  const [loading, setLoading] = useState(false);
  const [sending, setSending] = useState(false);
  const [revision, setRevision] = useState(0);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [selectedUf, setSelectedUf] = useState("SP");
  const [, setPreferenceRevision] = useState(0);
  const money = createCurrencyFormatter(getInterfacePreferences());

  useEffect(() => {
    const timer = window.setTimeout(() => setSku((current) => candidates.includes(current) ? current : candidates[0] || ""), 400);
    return () => window.clearTimeout(timer);
  }, [candidates]);

  useEffect(() => {
    const update = () => setPreferenceRevision((value) => value + 1);
    window.addEventListener(INTERFACE_PREFERENCES_EVENT, update);
    return () => window.removeEventListener(INTERFACE_PREFERENCES_EVENT, update);
  }, []);

  useEffect(() => {
    setMessage("");
  }, [sku, sources]);

  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    setData(null); setError("");
    if (!sku) { setLoading(false); return () => controller.abort(); }
    setLoading(true);
    const load = async () => {
      try {
        const next = await loadFreightMap(getApiBaseUrl(), sku, sources, controller.signal);
        if (controller.signal.aborted) return;
        setData(next);
        if (next.pending) timer = window.setTimeout(() => void load(), 3000);
      } catch (reason) {
        if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Falha ao carregar os fretes.");
      } finally { if (!controller.signal.aborted) setLoading(false); }
    };
    void load();
    return () => { controller.abort(); if (timer) window.clearTimeout(timer); };
  }, [sku, sources, revision]);

  const consult = async () => {
    if (!sku || sending || (data && !data.has_offers)) return;
    setSending(true); setError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/lead-time/${encodeURIComponent(sku)}/refresh`, {
        method: "POST", headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ states: shapes.map((state) => state.uf), force_refresh: false, sources: sources.map(canonicalFreightStore) }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "Falha ao consultar os fretes.");
      setRevision((value) => value + 1);
      setMessage(result.message);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "Falha na consulta."); }
    finally { setSending(false); }
  };

  const states = data?.states || [];
  const amounts = states.flatMap((state) => state.quote ? [state.quote.shipping_cost] : []);
  const low = Math.min(...amounts), high = Math.max(...amounts);
  const selected = states.find((state) => state.uf === selectedUf);
  const fill = (amount: number) => `hsl(192 70% ${80 - (high === low ? 0.5 : (amount - low) / (high - low)) * 46}%)`;

  return <section className="data-panel freight-map" aria-label="Mapa de fretes por estado">
    <div className="data-panel__header freight-map__header">
      <div><h2>Frete por estado</h2><p>CEP de referência de cada capital</p></div>
      <div className="freight-map__controls">
        {candidates.length > 1 && <StyledSelect ariaLabel="SKU do mapa de fretes" value={sku} onChange={setSku} options={candidates.map((value) => ({ value, label: value }))} />}
        <button type="button" className="toolbar-button" aria-label="Atualizar mapa de fretes" disabled={!sku || loading} onClick={() => setRevision((value) => value + 1)}><RefreshCw size={15} />Atualizar</button>
        {canRefresh && <button type="button" className="toolbar-button" disabled={!sku || loading || sending || !!data?.pending || (data != null && !data.has_offers)} title={data && !data.has_offers ? "Nenhuma oferta disponível para o SKU nas lojas selecionadas em Filtros" : undefined} onClick={() => void consult()}>{sending || data?.pending ? <LoaderCircle size={15} className="animate-spin" /> : <MapPin size={15} />}Consultar fretes</button>}
      </div>
    </div>
    {error && <p className="analytics-sheet__error" role="alert">{error}</p>}
    {message && <p className="analytics-sheet__status" role="status">{message}</p>}
    {!sku ? <div className="freight-map__empty"><MapPin size={24} /><span>Escolha um material para visualizar o frete por estado.</span></div> : loading && !data ? <div className="freight-map__empty" role="status"><LoaderCircle size={24} className="animate-spin" />Carregando fretes...</div> : data && <>
      <div className="freight-map__body">
        <div className="freight-map__graphic">
          <svg viewBox="0 0 610 590" role="group" aria-label={`Fretes de ${sku} nas 27 unidades da federação`}>
            {shapes.map((shape) => {
              const state = states.find((item) => item.uf === shape.uf);
              const quote = state?.quote;
              const label = `${state?.state || shape.uf}: ${quote ? money.format(quote.shipping_cost) : "Não cotado"}${quote?.stale ? " · Mais de 24h" : ""}`;
              return <g key={shape.uf}>
                <path d={shape.path} fill={quote ? fill(quote.shipping_cost) : "var(--surface-muted)"} className={selectedUf === shape.uf ? "freight-map__state is-selected" : "freight-map__state"} role="button" tabIndex={0} aria-label={label} aria-pressed={selectedUf === shape.uf} onClick={() => setSelectedUf(shape.uf)} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedUf(shape.uf); } }}><title>{label}</title></path>
                <text x={shape.label[0]} y={shape.label[1]} className="freight-map__label" aria-hidden="true">{shape.uf}</text>
              </g>;
            })}
          </svg>
          <div className="freight-map__legend"><span><i className="freight-map__no-quote" />Não cotado</span>{amounts.length > 0 && <span>{money.format(low)}<i className="freight-map__scale" />{money.format(high)}</span>}</div>
        </div>
        <div className="freight-map__details">
          {selected && <div className="freight-map__selected" aria-live="polite">
            <h3>{selected.state} <span>{selected.uf}</span></h3><p>{selected.capital} · CEP {formatCep(selected.postal_code)}</p>
            <strong className="freight-map__value">{selected.quote ? money.format(selected.quote.shipping_cost) : "Não cotado"}</strong>
            {selected.quote ? <><p>{selected.quote.source} · {selected.quote.seller}</p><small>{selected.quote.stale ? "Cotação com mais de 24h · " : ""}{formatDate(selected.quote.captured_at)}</small><a href={selected.quote.url} target="_blank" rel="noreferrer">Abrir oferta <ExternalLink size={13} /></a></> : <p>{data.has_offers ? "Consulte os fretes para obter uma cotação deste destino." : "Faça uma extração para localizar ofertas deste SKU."}</p>}
          </div>}
          <div className="freight-map__state-list" aria-label="Valores por estado">{states.map((state) => <button key={state.uf} type="button" className={selectedUf === state.uf ? "is-selected" : ""} onClick={() => setSelectedUf(state.uf)}><span>{state.uf}</span><strong>{state.quote ? money.format(state.quote.shipping_cost) : "Não cotado"}{state.quote?.stale && " *"}</strong></button>)}</div>
        </div>
      </div>
    </>}
  </section>;
}

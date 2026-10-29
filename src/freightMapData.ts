import { STORE_OPTIONS } from "./preferences";

export type FreightQuote = { shipping_cost: number; source: string; seller: string; url: string; captured_at: string; stale: boolean };
export type FreightState = { uf: string; state: string; capital: string; postal_code: string; quote: FreightQuote | null; quote_count: number };
export type FreightData = { sku: string; states: FreightState[]; sources: string[]; has_offers: boolean; pending: number };
type LegacyCell = Partial<FreightQuote> & { status: string; postal_code?: string; alternatives?: LegacyCell[]; pending?: number };
type LegacyMatrix = { sku: string; destinations: Omit<FreightState, "quote" | "quote_count">[]; stores: {
  name: string; offer_count: number; sellers: { name: string; cells: Record<string, LegacyCell> }[];
}[] };

export function canonicalFreightStore(value: string) {
  const normalize = (text: string) => text.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/[^a-z0-9]/g, "");
  return STORE_OPTIONS.find((store) => [store.label, ...store.aliases].some((alias) => normalize(alias) === normalize(value)))?.label || value;
}

// Compatibility with a backend started before the dedicated shipping route
// was installed. Matrix cells already enforce offer identity and exact CEP.
function fromMatrix(matrix: LegacyMatrix, sources: string[]): FreightData {
  const selected = new Set(sources.map(canonicalFreightStore));
  const stores = matrix.stores.filter((store) => !selected.size || selected.has(canonicalFreightStore(store.name)));
  let pending = 0;
  const states = matrix.destinations.map((dest) => {
    const values: FreightQuote[] = [];
    for (const store of stores) for (const seller of store.sellers) {
      const cell = seller.cells[dest.uf];
      if (!cell || cell.status === "out_of_stock") continue;
      const alternatives = cell.alternatives?.length ? cell.alternatives : [cell];
      for (const option of alternatives) {
        if (option.status === "queued" || option.status === "running") pending++;
        if (option.status !== "quoted" || option.postal_code !== dest.postal_code || option.shipping_cost == null || !Number.isFinite(option.shipping_cost) || option.shipping_cost < 0 || !option.captured_at || !option.url) continue;
        values.push({ shipping_cost: option.shipping_cost, captured_at: option.captured_at, stale: option.stale ?? true,
          source: store.name, seller: option.seller || seller.name, url: option.url });
      }
    }
    const fresh = values.filter((quote) => !quote.stale);
    const eligible = fresh.length ? fresh : values;
    const paid = eligible.filter((quote) => quote.shipping_cost > 0);
    const quote = (paid.length ? paid : eligible).reduce<FreightQuote | null>((best, item) => !best || item.shipping_cost < best.shipping_cost ? item : best, null);
    return { ...dest, quote, quote_count: values.length };
  });
  return { sku: matrix.sku, states, sources: matrix.stores.filter((store) => store.offer_count).map((store) => store.name),
    has_offers: stores.some((store) => store.offer_count > 0 && store.sellers.some((seller) => Object.values(seller.cells).some((cell) => cell.status !== "out_of_stock"))), pending };
}

export async function loadFreightMap(base: string, sku: string, sources: string[], signal: AbortSignal): Promise<FreightData> {
  const params = new URLSearchParams();
  sources.map(canonicalFreightStore).forEach((source) => params.append("source", source));
  const path = `${base}/products/lead-time/${encodeURIComponent(sku)}`;
  const response = await fetch(`${path}/shipping?${params}`, { signal });
  if (response.status === 404) {
    const matrix = await fetch(path, { signal });
    if (matrix.ok) return fromMatrix(await matrix.json() as LegacyMatrix, sources);
    throw new Error("Não foi possível carregar os fretes. Verifique se o backend está atualizado e tente novamente.");
  }
  if (!response.ok) throw new Error(`Não foi possível carregar o mapa de fretes (HTTP ${response.status}). Tente atualizar novamente.`);
  return await response.json() as FreightData;
}

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { MouseEvent as ReactMouseEvent } from "react";
import {
  ArrowLeft,
  ArrowUpDown,
  BarChart3,
  Boxes,
  Building2,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  FileSpreadsheet,
  Funnel,
  ImageIcon,
  LoaderCircle,
  PackageOpen,
  Search,
  Store,
  TrendingDown,
  X,
} from "lucide-react";
import type { ComparisonRow, Product } from "../models/product.model";
import { PriceChart } from "./PriceChart";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import {
  createCompactFormatter,
  createCurrencyFormatter,
  getInterfacePreferences,
  INTERFACE_PREFERENCES_EVENT,
  STORE_OPTIONS,
  type InterfacePreferences,
} from "../src/preferences";

interface SlidingTabsProps {
  activeTab: number;
  onTabChange: (tab: number) => void;
  loading: boolean;
  products: Product[];
  comparisonRows: ComparisonRow[];
  lastQuery: string;
}

type SortOrder = "stores_desc" | "price_asc" | "price_desc";
type DateRange = "all" | "today" | "7d" | "30d" | "90d";
type StoreRankingState = { row: ComparisonRow; left: number; top: number };

const OFFER_PAGE_SIZE = 5;
const CATALOG_PAGE_SIZE = 12;
const BRAND_FILTERS = ["Consul", "Brastemp", "Whirlpool"];
const STORE_FILTERS = STORE_OPTIONS;
const PRODUCT_STOP_WORDS = new Set([
  "a", "as", "com", "da", "de", "do", "dos", "e", "em", "na", "no", "os", "para", "por",
]);
const STORE_WORDS = new Set([
  "amazon", "click", "comclick", "dufrio", "duofrio", "friolar", "gold", "leroy", "magalu",
  "magazine", "mercado", "livre", "meli", "mg", "mota", "parts", "refrigeracao", "service", "shopee",
]);
const ACCESSORY_TERMS = new Set([
  "adaptador", "adesivo", "borracha", "bolsa", "cabo", "capa", "carregador", "case", "capacitor",
  "componentes", "controle", "estojo", "filtro", "fonte", "gaveta", "grade", "kit", "lampada",
  "mangueira", "motor", "painel", "pelicula", "peca", "pecas", "placa", "prateleira", "refil",
  "resistencia", "sensor", "suporte", "tampa", "termostato", "ventilador", "ventoinha", "vidro",
]);
const KNOWN_BRANDS = new Set([
  "apple", "brastemp", "consul", "continental", "electrolux", "elgin", "fast", "fischer", "hisense",
  "hq", "lg", "midea", "panasonic", "philco", "samsung", "shop", "whirlpool",
]);

const sortOptions: Array<{ value: SortOrder; label: string }> = [
  { value: "stores_desc", label: "Mais lojas" },
  { value: "price_asc", label: "Menor preço" },
  { value: "price_desc", label: "Maior preço" },
];

const dateOptions: Array<{ value: DateRange; label: string }> = [
  { value: "all", label: "Qualquer data" },
  { value: "today", label: "Hoje" },
  { value: "7d", label: "Últimos 7 dias" },
  { value: "30d", label: "Últimos 30 dias" },
  { value: "90d", label: "Últimos 90 dias" },
];

const offerDataOptions = [
  "Frete grátis",
  "Frete informado",
  "Lead time informado",
  "Pagamento a prazo",
];

const formatCurrency = (value: number) => createCurrencyFormatter(getInterfacePreferences()).format(value || 0);

const truncateText = (value: string, maxLength = 28) =>
  value.length > maxLength ? `${value.slice(0, maxLength - 3).trimEnd()}...` : value;

const normalize = (value?: string | null) =>
  (value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase();

const normalizeProductUnits = (name: string) =>
  normalize(name)
    .replace(/frostfree/g, "frost free")
    .replace(/(\d+)\s*(btus?|btu)\b/g, "$1btu")
    .replace(/(\d+)\s*(litros?|lts?|lt|l)\b/g, "$1l")
    .replace(/(\d+)\s*(volts?|volt|v)\b/g, "$1v")
    .replace(/(\d+)\s*(polegadas?|pol)\b/g, "$1pol")
    .replace(/(\d+)\s*(quilos?|kg)\b/g, "$1kg")
    .replace(/(\d+)\s*(gb|tb|ml|w)\b/g, "$1$2");

const productTokens = (name: string) => {
  const tokens = new Set(
    normalizeProductUnits(name)
      .match(/[a-z0-9]+/g)
      ?.filter(
        (token) =>
          token.length > 1 &&
          !PRODUCT_STOP_WORDS.has(token) &&
          !STORE_WORDS.has(token),
      ) || [],
  );

  if (tokens.has("geladeira") || tokens.has("geladeiras")) {
    tokens.add("geladeira");
    tokens.add("refrigerador");
  }
  if (tokens.has("refrigerador") || tokens.has("refrigeradores")) {
    tokens.add("geladeira");
    tokens.add("refrigerador");
  }
  return tokens;
};

const specTokens = (tokens: Set<string>) =>
  new Set([...tokens].filter((token) => /\d/.test(token)));
const brandTokens = (tokens: Set<string>) =>
  new Set([...tokens].filter((token) => KNOWN_BRANDS.has(token)));
const modelTokens = (tokens: Set<string>) =>
  new Set([...tokens].filter((token) => /[a-z]+\d|\d+[a-z]+/.test(token)));
const hasAccessoryToken = (tokens: Set<string>) =>
  [...tokens].some((token) => ACCESSORY_TERMS.has(token));

const sameProduct = (clusterTokens: Set<string>, itemTokens: Set<string>) => {
  if (!clusterTokens.size || !itemTokens.size) return false;
  if (hasAccessoryToken(clusterTokens) !== hasAccessoryToken(itemTokens)) return false;

  const clusterBrands = brandTokens(clusterTokens);
  const itemBrands = brandTokens(itemTokens);
  if (
    clusterBrands.size > 0 &&
    itemBrands.size > 0 &&
    ![...clusterBrands].some((token) => itemBrands.has(token))
  ) return false;

  const clusterModels = modelTokens(clusterTokens);
  const itemModels = modelTokens(itemTokens);
  if (
    clusterModels.size > 0 &&
    itemModels.size > 0 &&
    ![...clusterModels].some((token) => itemModels.has(token))
  ) return false;

  const clusterSpecs = specTokens(clusterTokens);
  const itemSpecs = specTokens(itemTokens);
  const bothHaveSpecs = clusterSpecs.size > 0 && itemSpecs.size > 0;
  if (bothHaveSpecs && ![...clusterSpecs].some((token) => itemSpecs.has(token))) return false;

  const shared = [...clusterTokens].filter((token) => itemTokens.has(token));
  const sharedNumbers = shared.filter((token) => /\d/.test(token));
  const unionScore = shared.length / Math.max(1, Math.max(clusterTokens.size, itemTokens.size));
  const containmentScore = shared.length / Math.max(1, Math.min(clusterTokens.size, itemTokens.size));

  return shared.length >= 3 && (
    unionScore >= 0.52 ||
    containmentScore >= 0.66 ||
    (sharedNumbers.length > 0 && containmentScore >= 0.55) ||
    (bothHaveSpecs && sharedNumbers.length > 0 && unionScore >= 0.42)
  );
};

const escapeRegExp = (value: string) =>
  value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const detectBrandInText = (value?: string | null) => {
  const text = normalize(value);
  const matches = BRAND_FILTERS.map((brand) => {
    const match = text.match(new RegExp(`\\b${escapeRegExp(normalize(brand))}\\b`));
    return match?.index === undefined ? null : { brand, index: match.index };
  }).filter((match): match is { brand: string; index: number } => Boolean(match));

  return matches.sort((a, b) => a.index - b.index)[0]?.brand || null;
};

const detectProductBrand = (product: Pick<Product, "name" | "brand">) =>
  detectBrandInText(product.name) || detectBrandInText(product.brand);

const getRowBrand = (row: ComparisonRow) => {
  const brand = row.offers.map(detectProductBrand).find(Boolean);
  return brand || detectBrandInText(row.name) || "—";
};

const productMatchesBrand = (product: Product, selectedBrands: string[]) =>
  selectedBrands.length === 0 || selectedBrands.includes(detectProductBrand(product) || "");

const productMatchesStore = (product: Product, selectedStores: string[]) => {
  if (selectedStores.length === 0) return true;
  const searchable = normalize(`${product.source || ""} ${product.store} ${product.url || ""}`);
  return selectedStores.some((store) => {
    const filter = STORE_FILTERS.find((item) => item.label === store);
    return (filter?.aliases || [store]).some((alias) => searchable.includes(normalize(alias)));
  });
};

const getRowImage = (row: ComparisonRow) =>
  row.offers.find((offer) => offer.image_url)?.image_url || null;

const bestOffersByStore = (offers: Product[]) => {
  const stores = new Map<string, Product>();

  offers.forEach((offer) => {
    const current = stores.get(offer.store);
    if (!current || offer.current_price < current.current_price) {
      stores.set(offer.store, offer);
    }
  });

  return [...stores.values()].sort((a, b) => a.current_price - b.current_price);
};

const roundCurrency = (value: number) => Math.round(value * 100) / 100;

const rebuildRowWithOffers = (
  row: ComparisonRow,
  offers: Product[],
): ComparisonRow | null => {
  const validOffers = offers
    .filter((offer) => Number.isFinite(Number(offer.current_price)) && Number(offer.current_price) > 0)
    .sort((a, b) => a.current_price - b.current_price);

  if (!validOffers.length) return null;

  const storeOffers = bestOffersByStore(validOffers);
  const cheapest = storeOffers[0];
  const pma = storeOffers.reduce((total, offer) => total + offer.current_price, 0) / storeOffers.length;
  const differenceValue = cheapest.current_price - pma;
  const differencePercent = pma > 0 ? Math.abs(differenceValue) / pma * 100 : 0;

  return {
    ...row,
    name: cheapest.name,
    sku: row.sku || cheapest.sku,
    description: row.description || cheapest.description,
    subfamily: row.subfamily || cheapest.subfamily,
    sku_category: row.sku_category || cheapest.sku_category,
    cluster: row.cluster || cheapest.cluster,
    market_type: row.market_type || cheapest.market_type,
    store_count: storeOffers.length,
    offer_count: validOffers.length,
    pma: roundCurrency(pma),
    cheapest_price: cheapest.current_price,
    cheapest_store: cheapest.store,
    cheapest_url: cheapest.url,
    difference_value: roundCurrency(differenceValue),
    difference_percent: roundCurrency(differencePercent),
    offers: validOffers,
  };
};

const splitRowsByBrand = (rows: ComparisonRow[]) =>
  rows.flatMap((row) => {
    const offers = row.offers || [];
    if (!offers.length) return [row];

    const offersByBrand = new Map<string, Product[]>();
    offers.forEach((offer) => {
      const brand = detectProductBrand(offer) || "Sem marca";
      offersByBrand.set(brand, [...(offersByBrand.get(brand) || []), offer]);
    });

    if (offersByBrand.size <= 1) return [row];

    return [...offersByBrand.values()]
      .map((brandOffers, index) =>
        rebuildRowWithOffers({ ...row, id: row.id * 100 + index + 1 }, brandOffers),
      )
      .filter((brandRow): brandRow is ComparisonRow => Boolean(brandRow));
  });

const buildRowFromOffers = (offers: Product[], id: number): ComparisonRow | null => {
  const first = offers[0];
  if (!first) return null;
  return rebuildRowWithOffers(
    {
      id,
      name: first.name,
      sku: first.sku,
      store_count: 0,
      offer_count: 0,
      pma: 0,
      cheapest_price: 0,
      cheapest_store: "",
      cheapest_url: "",
      difference_value: 0,
      difference_percent: 0,
      offers: [],
    },
    offers,
  );
};

const buildComparisonRows = (products: Product[]) => {
  const clusters: Array<{ tokens: Set<string>; offers: Product[] }> = [];

  products.forEach((product) => {
    const itemTokens = productTokens(product.name);
    const targetCluster = clusters.find((cluster) => sameProduct(cluster.tokens, itemTokens));

    if (targetCluster) {
      itemTokens.forEach((token) => targetCluster.tokens.add(token));
      targetCluster.offers.push(product);
      return;
    }

    clusters.push({ tokens: itemTokens, offers: [product] });
  });

  return clusters
    .map((cluster, index) => buildRowFromOffers(cluster.offers, index + 1))
    .filter((row): row is ComparisonRow => Boolean(row))
    .sort(
      (a, b) =>
        b.store_count - a.store_count ||
        b.offer_count - a.offer_count ||
        a.cheapest_price - b.cheapest_price,
    );
};

const formatDate = (value?: string) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("pt-BR", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
};

const productMatchesDate = (product: Product, dateRange: DateRange) => {
  if (dateRange === "all") return true;
  const updatedAt = new Date(product.last_update);
  if (Number.isNaN(updatedAt.getTime())) return false;

  const now = new Date();
  if (dateRange === "today") {
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return updatedAt >= startOfToday;
  }
  const days = dateRange === "7d" ? 7 : dateRange === "30d" ? 30 : 90;
  return updatedAt.getTime() >= now.getTime() - days * 24 * 60 * 60 * 1000;
};

const productMatchesMetadata = (
  product: Product,
  selectedSubfamilies: string[],
  selectedCategories: string[],
  selectedClusters: string[],
  selectedMarkets: string[],
) => (
  (selectedSubfamilies.length === 0 || selectedSubfamilies.includes(product.subfamily || "")) &&
  (selectedCategories.length === 0 || selectedCategories.includes(product.sku_category || "")) &&
  (selectedClusters.length === 0 || selectedClusters.includes(product.cluster || "")) &&
  (selectedMarkets.length === 0 || selectedMarkets.includes(product.market_type || ""))
);

const productMatchesOfferData = (product: Product, selectedData: string[]) =>
  selectedData.every((filter) => {
    if (filter === "Frete grátis") return Number(product.shipping_cost) === 0;
    if (filter === "Frete informado") return product.shipping_cost !== null && product.shipping_cost !== undefined;
    if (filter === "Lead time informado") {
      return (
        (product.delivery_min_days !== null && product.delivery_min_days !== undefined) ||
        (product.delivery_max_days !== null && product.delivery_max_days !== undefined)
      );
    }
    if (filter === "Pagamento a prazo") return Number(product.installment_price) > 0;
    return true;
  });

const paymentLabel = (offer: Product) => {
  const cash = offer.cash_price || offer.current_price;
  if (offer.installment_price && offer.installment_price > cash) {
    return `À vista · +${formatCurrency(offer.installment_price - cash)} a prazo`;
  }
  return "À vista";
};

const shippingLabel = (offer: Product) =>
  offer.shipping_cost === 0
    ? "Frete grátis"
    : offer.shipping_cost != null
      ? `Frete ${formatCurrency(offer.shipping_cost)}`
      : "Frete não informado";

const leadTimeLabel = (offer: Product) => {
  if (offer.delivery_min_days == null && offer.delivery_max_days == null) return "Prazo não informado";
  const minimum = offer.delivery_min_days ?? offer.delivery_max_days;
  const maximum = offer.delivery_max_days ?? offer.delivery_min_days;
  return minimum === maximum ? `${minimum} dias úteis` : `${minimum}-${maximum} dias úteis`;
};

const analyticsShippingLabel = (offer: Product) => {
  if (offer.shipping_cost == null) return "";
  return `Frete: ${formatCurrency(offer.shipping_cost)}`;
};

const analyticsLeadTimeLabel = (offer: Product) =>
  offer.delivery_min_days == null && offer.delivery_max_days == null ? "" : leadTimeLabel(offer);

const tableShippingLabel = (offer?: Product) => {
  if (offer?.shipping_cost == null) return "—";
  return offer.shipping_cost === 0 ? "Grátis" : formatCurrency(offer.shipping_cost);
};

const tableLeadTimeLabel = (offer?: Product) => {
  if (!offer || (offer.delivery_min_days == null && offer.delivery_max_days == null)) return "—";
  const minimum = offer.delivery_min_days ?? offer.delivery_max_days;
  const maximum = offer.delivery_max_days ?? offer.delivery_min_days;
  return minimum === maximum ? `${minimum} dias úteis` : `${minimum}-${maximum} dias úteis`;
};

const installmentDetails = (offers: Product[]) => {
  const offer = offers
    .filter((item) => Number(item.installment_price) > 0)
    .sort((left, right) => Number(left.installment_price) - Number(right.installment_price))[0];
  if (!offer?.installment_price) return null;
  const cashPrice = offer.cash_price || offer.current_price;
  const difference = offer.payment_difference ?? Math.max(0, offer.installment_price - cashPrice);
  return { price: offer.installment_price, difference };
};

const initials = (name: string) =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((word) => word[0])
    .join("")
    .toUpperCase();

const StoreMarks = ({ offers }: { offers: Product[] }) => {
  const stores = bestOffersByStore(offers).slice(0, 4);
  const remaining = Math.max(0, bestOffersByStore(offers).length - stores.length);

  return (
    <div className="flex items-center justify-center">
      {stores.map((offer, index) => (
        <span
          key={offer.store}
          title={offer.store}
          className="store-mark"
          style={{ zIndex: stores.length - index }}
        >
          {initials(offer.store)}
        </span>
      ))}
      {remaining > 0 && <span className="store-mark store-mark--more">+{remaining}</span>}
    </div>
  );
};

const StoreRankingPreview = ({
  row,
  left,
  top,
}: StoreRankingState) => {
  const offers = bestOffersByStore(row.offers || []).slice(0, 10);
  if (!offers.length) return null;

  return (
    <div className="store-ranking-popover" style={{ left, top, transform: "translateX(-50%)" }}>
      <div className="store-ranking-popover__header">
        <span>Ranking</span>
        <span>{offers.length} resultados</span>
      </div>
      {offers.map((offer, index) => (
        <div key={`${offer.store}-${offer.url}`} className="store-ranking-popover__row">
          <span className="store-ranking-popover__position">{String(index + 1).padStart(2, "0")}</span>
          <span className="min-w-0 flex-1 truncate">{offer.store}</span>
          <strong>{formatCurrency(offer.current_price)}</strong>
        </div>
      ))}
    </div>
  );
};

const EmptyState = ({ searched }: { searched: boolean }) => (
  <div className="flex min-h-[410px] flex-col items-center justify-center px-6 text-center">
    <span className="flex h-12 w-12 items-center justify-center rounded-lg border border-[var(--border)] bg-[var(--surface-subtle)] text-[var(--muted)]">
      <PackageOpen size={22} />
    </span>
    <h3 className="mt-4 text-sm font-semibold text-[var(--foreground)]">
      {searched ? "Nenhum resultado encontrado" : "Sua pesquisa começa aqui"}
    </h3>
    <p className="mt-1 max-w-sm text-xs leading-5 text-[var(--muted)]">
      {searched
        ? "Revise os filtros aplicados ou faça uma nova busca no campo superior."
        : "Pesquise um produto, modelo ou SKU para comparar ofertas entre as lojas monitoradas."}
    </p>
  </div>
);

const LoadingState = () => (
  <div className="overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--surface)]">
    <div className="flex h-16 items-center gap-3 border-b border-[var(--border)] px-5">
      <div className="skeleton h-9 w-64" />
      <div className="ml-auto skeleton h-9 w-28" />
      <div className="skeleton h-9 w-32" />
    </div>
    <div className="divide-y divide-[var(--border)]">
      {Array.from({ length: 8 }, (_, index) => (
        <div key={index} className="flex h-[74px] items-center gap-4 px-5">
          <div className="skeleton h-10 w-10 shrink-0" />
          <div className="min-w-0 flex-1">
            <div className="skeleton h-3.5 w-[min(360px,70%)]" />
            <div className="mt-2 skeleton h-3 w-28" />
          </div>
          <div className="skeleton hidden h-4 w-20 md:block" />
          <div className="skeleton hidden h-8 w-28 lg:block" />
        </div>
      ))}
    </div>
  </div>
);

export default function SlidingTabs({
  activeTab,
  onTabChange,
  loading,
  products,
  comparisonRows,
  lastQuery,
}: SlidingTabsProps) {
  const [resultSearch, setResultSearch] = useState("");
  const [sortOrder, setSortOrder] = useState<SortOrder>("stores_desc");
  const [selectedBrands, setSelectedBrands] = useState<string[]>([]);
  const [selectedStores, setSelectedStores] = useState<string[]>([]);
  const [selectedSubfamilies, setSelectedSubfamilies] = useState<string[]>([]);
  const [selectedCategories, setSelectedCategories] = useState<string[]>([]);
  const [selectedClusters, setSelectedClusters] = useState<string[]>([]);
  const [selectedMarkets, setSelectedMarkets] = useState<string[]>([]);
  const [selectedOfferData, setSelectedOfferData] = useState<string[]>([]);
  const [dateRange, setDateRange] = useState<DateRange>("all");
  const [priceRange, setPriceRange] = useState<[number, number]>([0, 0]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [sortOpen, setSortOpen] = useState(false);
  const [openFilterGroups, setOpenFilterGroups] = useState({
    price: true,
    date: false,
    brands: true,
    stores: true,
    subfamilies: false,
    categories: false,
    clusters: false,
    markets: false,
    offerData: false,
  });
  const [offerPage, setOfferPage] = useState(1);
  const [catalogPage, setCatalogPage] = useState(1);
  const [selectedRow, setSelectedRow] = useState<ComparisonRow | null>(null);
  const [hoveredStoreRanking, setHoveredStoreRanking] = useState<StoreRankingState | null>(null);
  const [interfacePreferences, setInterfacePreferences] = useState<InterfacePreferences>(getInterfacePreferences);
  const filterControlRef = useRef<HTMLDivElement>(null);
  const sortControlRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => {
    const sourceRows = comparisonRows.length
      ? comparisonRows
      : buildComparisonRows(products);
    return splitRowsByBrand(sourceRows);
  }, [comparisonRows, products]);

  const marketProducts = useMemo(() => {
    if (products.length) return products;
    const byUrl = new Map<string, Product>();
    rows.flatMap((row) => row.offers).forEach((product) => byUrl.set(product.url, product));
    return [...byUrl.values()];
  }, [products, rows]);

  const brands = BRAND_FILTERS;
  const stores = useMemo(() => {
    const labels = new Set(STORE_FILTERS.map((store) => store.label));
    marketProducts.forEach((product) => labels.add(product.source || product.store));
    return [...labels];
  }, [marketProducts]);
  const metadataOptions = useMemo(() => {
    const unique = (field: keyof Product) => [...new Set(
      marketProducts
        .map((product) => product[field])
        .filter((value): value is string => typeof value === "string" && Boolean(value.trim())),
    )].sort((left, right) => left.localeCompare(right, "pt-BR"));

    return {
      subfamilies: unique("subfamily"),
      categories: unique("sku_category"),
      clusters: unique("cluster"),
      markets: ["Cativo", "Competitivo"],
    };
  }, [marketProducts]);
  const priceBounds = useMemo(() => {
    const prices = marketProducts
      .map((product) => Number(product.current_price))
      .filter((price) => Number.isFinite(price) && price > 0);
    if (!prices.length) return { min: 0, max: 0 };
    return {
      min: Math.floor(Math.min(...prices)),
      max: Math.ceil(Math.max(...prices)),
    };
  }, [marketProducts]);
  const compactFormatter = useMemo(() => createCompactFormatter(interfacePreferences), [interfacePreferences]);
  const defaultStoreSelection = useMemo(
    () =>
      STORE_FILTERS.every((store) => interfacePreferences.defaultStores.includes(store.label))
        ? []
        : interfacePreferences.defaultStores.filter((store) => stores.includes(store)),
    [interfacePreferences.defaultStores, stores],
  );

  useEffect(() => {
    const closeControls = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!filterControlRef.current?.contains(target)) setFilterOpen(false);
      if (!sortControlRef.current?.contains(target)) setSortOpen(false);
    };

    document.addEventListener("pointerdown", closeControls, true);
    return () => document.removeEventListener("pointerdown", closeControls, true);
  }, []);

  useEffect(() => {
    const syncPreferences = () => {
      const nextPreferences = getInterfacePreferences();
      setInterfacePreferences(nextPreferences);
      setSelectedStores(
        STORE_FILTERS.every((store) => nextPreferences.defaultStores.includes(store.label))
          ? []
          : nextPreferences.defaultStores.filter((store) => stores.includes(store)),
      );
    };

    window.addEventListener(INTERFACE_PREFERENCES_EVENT, syncPreferences);
    return () => window.removeEventListener(INTERFACE_PREFERENCES_EVENT, syncPreferences);
  }, [stores]);

  useEffect(() => {
    setPriceRange([priceBounds.min, priceBounds.max]);
  }, [lastQuery, priceBounds.min, priceBounds.max]);

  useEffect(() => {
    setOfferPage(1);
    setCatalogPage(1);
  }, [
    resultSearch,
    selectedBrands,
    selectedStores,
    selectedSubfamilies,
    selectedCategories,
    selectedClusters,
    selectedMarkets,
    selectedOfferData,
    dateRange,
    priceRange,
    sortOrder,
    lastQuery,
  ]);

  useEffect(() => {
    setResultSearch("");
    setSelectedBrands([]);
    setSelectedStores(defaultStoreSelection);
    setSelectedSubfamilies([]);
    setSelectedCategories([]);
    setSelectedClusters([]);
    setSelectedMarkets([]);
    setSelectedOfferData([]);
    setDateRange("all");
    setSortOrder("stores_desc");
    setHoveredStoreRanking(null);
  }, [defaultStoreSelection, lastQuery]);

  useEffect(() => {
    if (activeTab !== 2) setSelectedRow(null);
    setHoveredStoreRanking(null);
  }, [activeTab]);

  const filteredRows = useMemo(() => {
    const query = normalize(resultSearch);
    const rowsWithOfferFilters = rows
      .map((row) => {
        const offers = (row.offers || [])
          .filter((offer) => productMatchesBrand(offer, selectedBrands))
          .filter((offer) => productMatchesStore(offer, selectedStores))
          .filter((offer) => productMatchesMetadata(
            offer,
            selectedSubfamilies,
            selectedCategories,
            selectedClusters,
            selectedMarkets,
          ))
          .filter((offer) => productMatchesDate(offer, dateRange))
          .filter((offer) => productMatchesOfferData(offer, selectedOfferData))
          .filter((offer) => {
            const price = Number(offer.current_price);
            return price >= priceRange[0] && price <= priceRange[1];
          });
        return offers.length ? rebuildRowWithOffers(row, offers) : null;
      })
      .filter((row): row is ComparisonRow => Boolean(row));

    const filtered = query
      ? rowsWithOfferFilters.filter((row) =>
          normalize(
            `${row.name} ${row.sku || ""} ${getRowBrand(row)} ${row.offers.map((offer) => offer.store).join(" ")}`,
          ).includes(query),
        )
      : rowsWithOfferFilters;

    return [...filtered].sort((a, b) => {
      if (sortOrder === "price_asc") return a.cheapest_price - b.cheapest_price;
      if (sortOrder === "price_desc") return b.cheapest_price - a.cheapest_price;
      return (
        b.store_count - a.store_count ||
        b.offer_count - a.offer_count ||
        a.cheapest_price - b.cheapest_price ||
        a.name.localeCompare(b.name, "pt-BR")
      );
    });
  }, [
    rows,
    resultSearch,
    selectedBrands,
    selectedStores,
    selectedSubfamilies,
    selectedCategories,
    selectedClusters,
    selectedMarkets,
    selectedOfferData,
    dateRange,
    priceRange,
    sortOrder,
  ]);

  const filteredProducts = useMemo(() => {
    const query = normalize(resultSearch);
    const filtered = marketProducts.filter((product) => {
      const matchesQuery =
        !query ||
        normalize(`${product.name} ${product.brand} ${product.sku || ""} ${product.store}`).includes(query);
      const matchesBrand = productMatchesBrand(product, selectedBrands);
      const matchesStore = productMatchesStore(product, selectedStores);
      const matchesMetadata = productMatchesMetadata(
        product,
        selectedSubfamilies,
        selectedCategories,
        selectedClusters,
        selectedMarkets,
      );
      const matchesDate = productMatchesDate(product, dateRange);
      const matchesOfferData = productMatchesOfferData(product, selectedOfferData);
      const price = Number(product.current_price);
      const matchesPrice = price >= priceRange[0] && price <= priceRange[1];
      return matchesQuery && matchesBrand && matchesStore && matchesMetadata && matchesDate && matchesOfferData && matchesPrice;
    });

    return [...filtered].sort((a, b) => {
      if (sortOrder === "price_desc") return b.current_price - a.current_price;
      if (sortOrder === "stores_desc") return a.store.localeCompare(b.store, "pt-BR");
      return a.current_price - b.current_price;
    });
  }, [
    marketProducts,
    resultSearch,
    selectedBrands,
    selectedStores,
    selectedSubfamilies,
    selectedCategories,
    selectedClusters,
    selectedMarkets,
    selectedOfferData,
    dateRange,
    priceRange,
    sortOrder,
  ]);

  useEffect(() => {
    const totalPages = Math.max(1, Math.ceil(filteredRows.length / OFFER_PAGE_SIZE));
    setOfferPage((currentPage) => Math.min(currentPage, totalPages));
  }, [filteredRows.length]);

  useEffect(() => {
    const totalPages = Math.max(1, Math.ceil(filteredProducts.length / CATALOG_PAGE_SIZE));
    setCatalogPage((currentPage) => Math.min(currentPage, totalPages));
  }, [filteredProducts.length]);

  const allPrices = filteredProducts
    .map((product) => Number(product.current_price))
    .filter((price) => Number.isFinite(price) && price > 0);
  const lowestPrice = allPrices.length ? Math.min(...allPrices) : 0;
  const averagePrice = allPrices.length
    ? allPrices.reduce((sum, price) => sum + price, 0) / allPrices.length
    : 0;
  const priceFilterActive = priceRange[0] > priceBounds.min || priceRange[1] < priceBounds.max;
  const activeFilterCount =
    selectedBrands.length +
    selectedStores.length +
    selectedSubfamilies.length +
    selectedCategories.length +
    selectedClusters.length +
    selectedMarkets.length +
    selectedOfferData.length +
    (dateRange === "all" ? 0 : 1) +
    (priceFilterActive ? 1 : 0);
  const availableStores = new Set(filteredProducts.map((product) => product.store)).size;
  const sortLabel = sortOptions.find((option) => option.value === sortOrder)?.label;

  const marketStoreRanking = useMemo(() => {
    const ranked = new Map<string, { name: string; offers: number; lowest: number }>();
    filteredProducts.forEach((product) => {
      const current = ranked.get(product.store);
      ranked.set(product.store, {
        name: product.store,
        offers: (current?.offers || 0) + 1,
        lowest: Math.min(current?.lowest ?? Number.POSITIVE_INFINITY, product.current_price),
      });
    });
    return [...ranked.values()].sort((a, b) => b.offers - a.offers || a.lowest - b.lowest);
  }, [filteredProducts]);

  const toggleFilter = (
    value: string,
    selected: string[],
    setter: React.Dispatch<React.SetStateAction<string[]>>,
  ) => {
    setter(selected.includes(value) ? selected.filter((item) => item !== value) : [...selected, value]);
  };

  const openAnalytics = (row: ComparisonRow) => {
    setSelectedRow(row);
    onTabChange(2);
  };

  const clearFilters = () => {
    setSelectedBrands([]);
    setSelectedStores(defaultStoreSelection);
    setSelectedSubfamilies([]);
    setSelectedCategories([]);
    setSelectedClusters([]);
    setSelectedMarkets([]);
    setSelectedOfferData([]);
    setDateRange("all");
    setPriceRange([priceBounds.min, priceBounds.max]);
  };

  const showStoreRanking = (
    row: ComparisonRow,
    event: ReactMouseEvent<HTMLDivElement>,
  ) => {
    const rect = event.currentTarget.getBoundingClientRect();
    const panelWidth = Math.min(360, window.innerWidth - 24);
    const offerCount = Math.min(10, bestOffersByStore(row.offers || []).length);
    const panelHeight = 43 + offerCount * 39;
    const left = Math.min(
      Math.max(rect.left + rect.width / 2, panelWidth / 2 + 14),
      window.innerWidth - panelWidth / 2 - 14,
    );
    const fitsBelow = rect.bottom + panelHeight + 12 < window.innerHeight;
    const top = fitsBelow ? rect.bottom + 7 : Math.max(72, rect.top - panelHeight - 7);
    setHoveredStoreRanking({ row, left, top });
  };

  const metricCards = [
    {
      label: "Ofertas",
      value: compactFormatter.format(filteredProducts.length),
      helper: `${filteredRows.length} produtos comparáveis`,
      icon: Boxes,
      tone: "blue",
    },
    {
      label: "Lojas ativas",
      value: compactFormatter.format(availableStores),
      helper: availableStores ? `${availableStores} fontes encontradas` : "Aguardando pesquisa",
      icon: Building2,
      tone: "violet",
    },
    {
      label: "Menor preço",
      value: lowestPrice ? formatCurrency(lowestPrice) : "—",
      helper: "Melhor preço disponível",
      icon: TrendingDown,
      tone: "green",
    },
    {
      label: "Preço médio",
      value: averagePrice ? formatCurrency(averagePrice) : "—",
      helper: allPrices.length ? `${allPrices.length} preços analisados` : "Sem dados ainda",
      icon: BarChart3,
      tone: "amber",
    },
  ];

  return (
    <div className="mx-auto w-full max-w-[1600px]">
      {!selectedRow && (
        <section className="metrics-grid" aria-label="Resumo da pesquisa">
          {metricCards.map((metric) => {
            const Icon = metric.icon;
            return (
              <article key={metric.label} className="metric-card">
                <div>
                  <p className="metric-card__label">{metric.label}</p>
                  <p className="metric-card__value">{metric.value}</p>
                  <p className="metric-card__helper">{metric.helper}</p>
                </div>
                <span className={`metric-card__icon metric-card__icon--${metric.tone}`}>
                  <Icon size={18} />
                </span>
              </article>
            );
          })}
        </section>
      )}

      {loading ? (
        <div className="mt-5"><LoadingState /></div>
      ) : selectedRow && activeTab === 2 ? (
        <ProductAnalytics row={selectedRow} onBack={() => setSelectedRow(null)} />
      ) : (
        <>
          <div className="workspace-toolbar">
            <div className="result-search">
              <Search size={16} />
              <input
                type="search"
                value={resultSearch}
                onChange={(event) => setResultSearch(event.target.value)}
                placeholder="Filtrar resultados..."
                aria-label="Filtrar resultados"
              />
              {resultSearch && (
                <button type="button" onClick={() => setResultSearch("")} aria-label="Limpar filtro textual">
                  <X size={14} />
                </button>
              )}
            </div>

            <div ref={filterControlRef} className="relative ml-auto">
              <button
                type="button"
                className={`toolbar-button ${filterOpen ? "toolbar-button--active" : ""}`}
                onClick={() => {
                  setFilterOpen((open) => !open);
                  setSortOpen(false);
                }}
              >
                <Funnel size={15} />
                Filtros
                {activeFilterCount > 0 && <span className="filter-count">{activeFilterCount}</span>}
              </button>

              {filterOpen && (
                <div className="filter-popover">
                  <div className="flex items-center justify-between border-b border-[var(--border)] px-4 py-3">
                    <div>
                      <p className="text-xs font-semibold text-[var(--foreground)]">Filtrar resultados</p>
                    </div>
                    {activeFilterCount > 0 && (
                      <button type="button" className="text-xs font-semibold text-[var(--accent)]" onClick={clearFilters}>
                        Limpar
                      </button>
                    )}
                  </div>
                  <div className="filter-sections">
                    <PriceRangeFilter
                      min={priceBounds.min}
                      max={priceBounds.max}
                      value={priceRange}
                      open={openFilterGroups.price}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, price: !groups.price }))
                      }
                      onChange={setPriceRange}
                    />
                    <DateFilterColumn
                      value={dateRange}
                      open={openFilterGroups.date}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, date: !groups.date }))
                      }
                      onChange={setDateRange}
                    />
                    <FilterColumn
                      title="Dados da oferta"
                      items={offerDataOptions}
                      selected={selectedOfferData}
                      open={openFilterGroups.offerData}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, offerData: !groups.offerData }))
                      }
                      onToggle={(value) => toggleFilter(value, selectedOfferData, setSelectedOfferData)}
                    />
                    <FilterColumn
                      title="Marcas"
                      items={brands}
                      selected={selectedBrands}
                      open={openFilterGroups.brands}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, brands: !groups.brands }))
                      }
                      onToggle={(value) => toggleFilter(value, selectedBrands, setSelectedBrands)}
                    />
                    <FilterColumn
                      title="Lojas"
                      items={stores}
                      selected={selectedStores}
                      open={openFilterGroups.stores}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, stores: !groups.stores }))
                      }
                      onToggle={(value) => toggleFilter(value, selectedStores, setSelectedStores)}
                    />
                    <FilterColumn
                      title="Subfamília"
                      items={metadataOptions.subfamilies}
                      selected={selectedSubfamilies}
                      open={openFilterGroups.subfamilies}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, subfamilies: !groups.subfamilies }))
                      }
                      onToggle={(value) => toggleFilter(value, selectedSubfamilies, setSelectedSubfamilies)}
                    />
                    <FilterColumn
                      title="Categoria"
                      items={metadataOptions.categories}
                      selected={selectedCategories}
                      open={openFilterGroups.categories}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, categories: !groups.categories }))
                      }
                      onToggle={(value) => toggleFilter(value, selectedCategories, setSelectedCategories)}
                    />
                    <FilterColumn
                      title="Cluster"
                      items={metadataOptions.clusters}
                      selected={selectedClusters}
                      open={openFilterGroups.clusters}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, clusters: !groups.clusters }))
                      }
                      onToggle={(value) => toggleFilter(value, selectedClusters, setSelectedClusters)}
                    />
                    <FilterColumn
                      title="Mercado"
                      items={metadataOptions.markets}
                      selected={selectedMarkets}
                      open={openFilterGroups.markets}
                      onOpenChange={() =>
                        setOpenFilterGroups((groups) => ({ ...groups, markets: !groups.markets }))
                      }
                      onToggle={(value) => toggleFilter(value, selectedMarkets, setSelectedMarkets)}
                    />
                  </div>
                </div>
              )}
            </div>

            <div ref={sortControlRef} className="relative">
              <button
                type="button"
                className={`toolbar-button min-w-[138px] justify-between ${sortOpen ? "toolbar-button--active" : ""}`}
                onClick={() => {
                  setSortOpen((open) => !open);
                  setFilterOpen(false);
                }}
              >
                <span className="flex items-center gap-2">
                  <ArrowUpDown size={15} />
                  {sortLabel}
                </span>
                <ChevronDown size={14} className={`transition-transform duration-200 ${sortOpen ? "rotate-180" : ""}`} />
              </button>
              {sortOpen && (
                <div className="sort-menu">
                  {sortOptions.map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      onClick={() => {
                        setSortOrder(option.value);
                        setSortOpen(false);
                      }}
                      className={sortOrder === option.value ? "sort-menu__item--active" : ""}
                    >
                      <span>{option.label}</span>
                      {sortOrder === option.value && <Check size={14} />}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {activeTab === 0 && (
            <OffersTable
              rows={filteredRows.slice(
                (offerPage - 1) * OFFER_PAGE_SIZE,
                offerPage * OFFER_PAGE_SIZE,
              )}
              total={filteredRows.length}
              page={offerPage}
              pageSize={OFFER_PAGE_SIZE}
              searched={Boolean(lastQuery)}
              onAnalytics={openAnalytics}
              onStoreEnter={showStoreRanking}
              onStoreLeave={() => setHoveredStoreRanking(null)}
              onPageChange={setOfferPage}
            />
          )}

          {activeTab === 1 && (
            <CatalogGrid
              products={filteredProducts.slice(
                (catalogPage - 1) * CATALOG_PAGE_SIZE,
                catalogPage * CATALOG_PAGE_SIZE,
              )}
              total={filteredProducts.length}
              page={catalogPage}
              pageSize={CATALOG_PAGE_SIZE}
              searched={Boolean(lastQuery)}
              onPageChange={setCatalogPage}
            />
          )}

          {activeTab === 2 && (
            <MarketAnalytics
              products={filteredProducts}
              rows={filteredRows}
              stores={marketStoreRanking}
              searched={Boolean(lastQuery)}
              query={lastQuery}
              onOpenProduct={openAnalytics}
            />
          )}
        </>
      )}
      {hoveredStoreRanking &&
        createPortal(<StoreRankingPreview {...hoveredStoreRanking} />, document.body)}
    </div>
  );
}

const PriceRangeFilter = ({
  min,
  max,
  value,
  open,
  onOpenChange,
  onChange,
}: {
  min: number;
  max: number;
  value: [number, number];
  open: boolean;
  onOpenChange: () => void;
  onChange: React.Dispatch<React.SetStateAction<[number, number]>>;
}) => {
  const active = value[0] > min || value[1] < max;

  return (
    <div className="filter-section">
      <button type="button" className="filter-section__trigger" onClick={onOpenChange}>
        <span>Faixa de preço</span>
        <span className="flex items-center gap-2">
          {active && <span className="filter-section__count">1</span>}
          <ChevronDown size={15} className={`transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
        </span>
      </button>
      {open && (
        <div className="price-range-filter">
          <div className="price-range-filter__values">
            <span>{formatCurrency(value[0])}</span>
            <span>{formatCurrency(value[1])}</span>
          </div>
          <div className="price-range-filter__sliders">
            <input
              type="range"
              min={min}
              max={Math.max(min, max)}
              step="1"
              value={value[0]}
              disabled={max <= min}
              aria-label="Preço mínimo"
              onChange={(event) => {
                const next = Math.min(Number(event.target.value), value[1]);
                onChange([next, value[1]]);
              }}
            />
            <input
              type="range"
              min={min}
              max={Math.max(min, max)}
              step="1"
              value={value[1]}
              disabled={max <= min}
              aria-label="Preço máximo"
              onChange={(event) => {
                const next = Math.max(Number(event.target.value), value[0]);
                onChange([value[0], next]);
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
};

const DateFilterColumn = ({
  value,
  open,
  onOpenChange,
  onChange,
}: {
  value: DateRange;
  open: boolean;
  onOpenChange: () => void;
  onChange: (value: DateRange) => void;
}) => (
  <div className="filter-section">
    <button type="button" className="filter-section__trigger" onClick={onOpenChange}>
      <span>Data de atualização</span>
      <span className="flex items-center gap-2">
        {value !== "all" && <span className="filter-section__count">1</span>}
        <ChevronDown size={15} className={`transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </span>
    </button>
    {open && (
      <div className="filter-section__content">
        {dateOptions.map((option) => (
          <button
            key={option.value}
            type="button"
            className="filter-option"
            onClick={() => onChange(option.value)}
          >
            <span className={`filter-radio ${value === option.value ? "filter-radio--active" : ""}`} />
            <span>{option.label}</span>
          </button>
        ))}
      </div>
    )}
  </div>
);

const FilterColumn = ({
  title,
  items,
  selected,
  open,
  onOpenChange,
  onToggle,
}: {
  title: string;
  items: string[];
  selected: string[];
  open: boolean;
  onOpenChange: () => void;
  onToggle: (value: string) => void;
}) => (
  <div className="filter-section">
    <button type="button" className="filter-section__trigger" onClick={onOpenChange}>
      <span>{title}</span>
      <span className="flex items-center gap-2">
        {selected.length > 0 && <span className="filter-section__count">{selected.length}</span>}
        <ChevronDown size={15} className={`transition-transform duration-200 ${open ? "rotate-180" : ""}`} />
      </span>
    </button>

    {open && (
      <div className="filter-section__content">
        {items.length ? (
          items.map((item) => {
            const active = selected.includes(item);
            return (
              <button key={item} type="button" onClick={() => onToggle(item)} className="filter-option">
                <span className={`filter-checkbox ${active ? "filter-checkbox--active" : ""}`}>
                  {active && <Check size={11} strokeWidth={3} />}
                </span>
                <span className="truncate">{item}</span>
              </button>
            );
          })
        ) : (
          <p className="px-1 py-3 text-xs text-[var(--muted)]">Sem opções</p>
        )}
      </div>
    )}
  </div>
);

const OffersTable = ({
  rows,
  total,
  searched,
  onAnalytics,
  onStoreEnter,
  onStoreLeave,
  page,
  pageSize,
  onPageChange,
}: {
  rows: ComparisonRow[];
  total: number;
  page: number;
  pageSize: number;
  searched: boolean;
  onAnalytics: (row: ComparisonRow) => void;
  onStoreEnter: (row: ComparisonRow, event: ReactMouseEvent<HTMLDivElement>) => void;
  onStoreLeave: () => void;
  onPageChange: (page: number) => void;
}) => (
  <section className="data-panel">
    <div className="data-panel__header">
      <div>
        <h2>Lista de produtos</h2>
        <p>{total} {total === 1 ? "produto encontrado" : "produtos encontrados"}</p>
      </div>
    </div>

    {rows.length ? (
      <>
        <div className="overflow-x-auto">
          <table className="offer-table">
            <thead>
              <tr>
                <th className="w-[30%]">Produto</th>
                <th>Marca / SKU</th>
                <th className="text-center">Lojas</th>
                <th className="text-center">Preço médio</th>
                <th className="text-center">Melhor preço</th>
                <th className="text-center">Á PRAZO</th>
                <th className="text-center">Frete</th>
                <th className="text-center">Lead time</th>
                <th>Atualização</th>
                <th className="w-14" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const image = getRowImage(row);
                const cheapestOffer = bestOffersByStore(row.offers)[0];
                const installment = installmentDetails(row.offers);
                return (
                  <tr key={`${row.id}-${row.name}`}>
                    <td>
                      <div className="flex min-w-0 items-center gap-3">
                        <div className="product-thumb">
                          {image ? <img src={image} alt="" loading="lazy" /> : <ImageIcon size={18} />}
                        </div>
                        <div className="min-w-0">
                          <p className="line-clamp-2 text-sm font-semibold leading-[1.35rem] text-[var(--foreground)]">
                            {row.name}
                          </p>
                          <p className="mt-0.5 text-[13px] text-[var(--muted)]">
                            {row.offer_count} {row.offer_count === 1 ? "oferta analisada" : "ofertas analisadas"}
                          </p>
                        </div>
                      </div>
                    </td>
                    <td>
                      <p className="text-sm font-medium text-[var(--foreground)]">{getRowBrand(row)}</p>
                      <p className="mt-1 font-mono text-[13px] text-[var(--muted)]">{row.sku || "SKU não informado"}</p>
                    </td>
                    <td className="text-center">
                      <div
                        className="inline-flex cursor-help flex-col items-center"
                        onMouseEnter={(event) => onStoreEnter(row, event)}
                        onMouseLeave={onStoreLeave}
                      >
                        <StoreMarks offers={row.offers} />
                        <p className="mt-1 text-[13px] text-[var(--muted)]">{row.store_count} lojas</p>
                      </div>
                    </td>
                    <td className="text-center">
                      <span className="text-sm font-medium tabular-nums text-[var(--muted-strong)]">{formatCurrency(row.pma)}</span>
                    </td>
                    <td className="text-center">
                      <a href={row.cheapest_url} target="_blank" rel="noreferrer" className="best-price-link">
                        <span>
                          <span className="best-price-link__value">
                            <strong>{formatCurrency(row.cheapest_price)}</strong>
                            <ExternalLink size={13} />
                          </span>
                          <small title={row.cheapest_store}>{truncateText(row.cheapest_store)}</small>
                        </span>
                      </a>
                    </td>
                    <td className="text-center">
                      {installment ? (
                        <span className="offer-data-cell">
                          <strong>{formatCurrency(installment.price)}</strong>
                          <small>+{formatCurrency(installment.difference)} vs. à vista</small>
                        </span>
                      ) : (
                        <span className="offer-data-cell offer-data-cell--empty">—</span>
                      )}
                    </td>
                    <td className="text-center">
                      <span className="offer-data-cell">
                        <strong>{tableShippingLabel(cheapestOffer)}</strong>
                      </span>
                    </td>
                    <td className="text-center">
                      <span className="offer-data-cell">
                        <strong>{tableLeadTimeLabel(cheapestOffer)}</strong>
                      </span>
                    </td>
                    <td>
                      <span className="text-[13px] text-[var(--muted)]">{formatDate(row.offers[0]?.last_update)}</span>
                    </td>
                    <td>
                      <button type="button" className="table-action" onClick={() => onAnalytics(row)} aria-label="Abrir analytics" title="Abrir analytics">
                        <BarChart3 size={17} />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <PaginationFooter
          page={page}
          pageSize={pageSize}
          total={total}
          onPageChange={onPageChange}
        />
      </>
    ) : (
      <EmptyState searched={searched} />
    )}
  </section>
);

const PaginationFooter = ({
  page,
  pageSize,
  total,
  onPageChange,
}: {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (page: number) => void;
}) => {
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const visibleCount = Math.min(5, totalPages);
  const firstPage = Math.max(1, Math.min(page - 2, totalPages - visibleCount + 1));
  const pages = Array.from({ length: visibleCount }, (_, index) => firstPage + index);
  const rangeStart = total ? (page - 1) * pageSize + 1 : 0;
  const rangeEnd = Math.min(page * pageSize, total);

  return (
    <div className="panel-footer panel-footer--pagination">
      <p>Exibindo <strong>{rangeStart}-{rangeEnd}</strong> de <strong>{total}</strong></p>
      <nav className="pagination" aria-label="Paginação de produtos">
        <button
          type="button"
          onClick={() => onPageChange(page - 1)}
          disabled={page <= 1}
          aria-label="Página anterior"
        >
          <ChevronLeft size={15} />
        </button>
        {pages.map((pageNumber) => (
          <button
            key={pageNumber}
            type="button"
            className={pageNumber === page ? "pagination__page pagination__page--active" : "pagination__page"}
            onClick={() => onPageChange(pageNumber)}
            aria-current={pageNumber === page ? "page" : undefined}
          >
            {pageNumber}
          </button>
        ))}
        <button
          type="button"
          onClick={() => onPageChange(page + 1)}
          disabled={page >= totalPages}
          aria-label="Próxima página"
        >
          <ChevronRight size={15} />
        </button>
      </nav>
    </div>
  );
};

const CatalogGrid = ({
  products,
  total,
  page,
  pageSize,
  searched,
  onPageChange,
}: {
  products: Product[];
  total: number;
  page: number;
  pageSize: number;
  searched: boolean;
  onPageChange: (page: number) => void;
}) => (
  <section className="data-panel">
    <div className="data-panel__header">
      <div>
        <h2>Todos os itens</h2>
        <p>{total} {total === 1 ? "oferta no catálogo" : "ofertas no catálogo"}</p>
      </div>
    </div>
    {products.length ? (
      <>
        <div className="catalog-grid">
          {products.map((product) => (
            <article key={`${product.id}-${product.url}`} className="catalog-card">
              <div className="catalog-card__media">
                {product.image_url ? (
                  <img src={product.image_url} alt={product.name} loading="lazy" />
                ) : (
                  <ImageIcon size={28} />
                )}
                <span className="catalog-card__store">{product.store}</span>
              </div>
              <div className="flex flex-1 flex-col p-4">
                <div className="flex items-center gap-2 text-xs font-semibold uppercase text-[var(--muted)]">
                  <span>{product.brand || "Sem marca"}</span>
                </div>
                <h3 className="mt-2 line-clamp-2 min-h-[42px] text-[13px] font-semibold leading-[1.35rem] text-[var(--foreground)]">
                  {product.name}
                </h3>
                <div className="mt-auto flex items-end justify-between gap-3 pt-5">
                  <div>
                    <p className="text-xs font-medium text-[var(--muted)]">Preço atual</p>
                    <p className="mt-0.5 text-lg font-semibold tracking-tight text-[var(--foreground)]">{formatCurrency(product.current_price)}</p>
                    <p className="mt-1 text-xs font-medium text-[var(--muted)]">{paymentLabel(product)}</p>
                    <p className="mt-0.5 text-xs text-[var(--muted)]">{shippingLabel(product)} · {leadTimeLabel(product)}</p>
                  </div>
                  <a href={product.url} target="_blank" rel="noreferrer" className="catalog-link" aria-label={`Abrir ${product.name}`}>
                    <ExternalLink size={15} />
                  </a>
                </div>
              </div>
            </article>
          ))}
        </div>
        <PaginationFooter
          page={page}
          pageSize={pageSize}
          total={total}
          onPageChange={onPageChange}
        />
      </>
    ) : (
      <EmptyState searched={searched} />
    )}
  </section>
);

const MarketAnalytics = ({
  products,
  rows,
  stores,
  searched,
  query,
  onOpenProduct,
}: {
  products: Product[];
  rows: ComparisonRow[];
  stores: Array<{ name: string; offers: number; lowest: number }>;
  searched: boolean;
  query: string;
  onOpenProduct: (row: ComparisonRow) => void;
}) => {
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");

  if (!products.length) return <section className="data-panel"><EmptyState searched={searched} /></section>;

  const maxOffers = Math.max(...stores.map((store) => store.offers), 1);

  const exportAnalytics = async () => {
    if (!rows.length || exporting) return;

    setExporting(true);
    setExportError("");
    try {
      const response = await fetch(`${getApiBaseUrl()}/products/export/analytics`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, rows }),
      });

      if (!response.ok) {
        const error = await response.json().catch(() => null);
        throw new Error(error?.detail || "Não foi possível exportar os dados.");
      }

      const blob = await response.blob();
      const disposition = response.headers.get("Content-Disposition") || "";
      const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1] || "nevetracker-analytics.xlsx";
      const downloadUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = downloadUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(downloadUrl);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : "Não foi possível exportar os dados.");
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="analytics-view">
      <div className="analytics-layout">
        <section className="data-panel analytics-chart-panel">
          <div className="data-panel__header">
            <div><h2>Preço mínimo por loja</h2><p>Melhor condição encontrada em cada fonte</p></div>
            <div className="analytics-export-control">
              {exportError && <span role="alert">{exportError}</span>}
              <button
                type="button"
                className="icon-button analytics-export-button"
                onClick={() => void exportAnalytics()}
                disabled={exporting || !rows.length}
                aria-label="Exportar analytics para Excel"
                title="Exportar para Excel"
              >
                {exporting ? <LoaderCircle size={17} className="animate-spin" /> : <FileSpreadsheet size={17} />}
              </button>
            </div>
          </div>
          <div className="p-4 sm:p-5"><PriceChart products={products} /></div>
        </section>

        <section className="data-panel">
          <div className="data-panel__header">
            <div><h2>Cobertura das lojas</h2><p>Participação nas ofertas encontradas</p></div>
          </div>
          <div className="store-ranking-list">
            {stores.slice(0, 8).map((store, index) => (
              <div key={store.name} className="store-ranking-row">
                <span className="store-ranking-index">{String(index + 1).padStart(2, "0")}</span>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center justify-between gap-3">
                    <span className="truncate text-xs font-semibold text-[var(--foreground)]">{store.name}</span>
                    <span className="text-xs font-medium text-[var(--muted)]">{store.offers} ofertas</span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-[var(--surface-muted)]">
                    <div className="h-full rounded-full bg-[var(--accent)]" style={{ width: `${Math.max(8, store.offers / maxOffers * 100)}%` }} />
                  </div>
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="data-panel analytics-products-panel">
          <div className="data-panel__header">
            <div><h2>Produtos em destaque</h2><p>Itens com maior cobertura entre lojas</p></div>
          </div>
          <div className="divide-y divide-[var(--border)]">
            {rows.slice(0, 6).map((row) => (
              <button key={`${row.id}-${row.name}`} type="button" className="analytics-product-row" onClick={() => onOpenProduct(row)}>
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-[var(--surface-muted)] text-[var(--muted)]">
                  <Store size={16} />
                </span>
                <span className="min-w-0 flex-1 text-left">
                  <strong className="block truncate text-xs font-semibold text-[var(--foreground)]">{row.name}</strong>
                  <small className="mt-0.5 block text-xs text-[var(--muted)]">{row.store_count} lojas · {row.offer_count} ofertas</small>
                </span>
                <span className="text-right">
                  <strong className="block text-xs font-semibold text-[var(--success)]">{formatCurrency(row.cheapest_price)}</strong>
                  <small className="mt-0.5 block text-xs text-[var(--muted)]">Melhor preço</small>
                </span>
                <ChevronDown size={15} className="-rotate-90 text-[var(--muted)]" />
              </button>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
};

const ProductAnalytics = ({ row, onBack }: { row: ComparisonRow; onBack: () => void }) => {
  const offers = bestOffersByStore(row.offers);
  const spread = offers.length > 1 ? offers[offers.length - 1].current_price - offers[0].current_price : 0;

  return (
    <div className="mt-5 animate-enter">
      <button type="button" onClick={onBack} className="back-button"><ArrowLeft size={15} />Voltar para visão geral</button>
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        <article className="detail-stat"><span>Melhor preço</span><strong className="text-[var(--success)]">{formatCurrency(row.cheapest_price)}</strong><small>{row.cheapest_store}</small></article>
        <article className="detail-stat"><span>Preço médio</span><strong>{formatCurrency(row.pma)}</strong><small>{row.offer_count} ofertas analisadas</small></article>
        <article className="detail-stat"><span>Amplitude</span><strong>{formatCurrency(spread)}</strong><small>Entre menor e maior preço</small></article>
      </div>
      <div className="mt-4 grid items-start gap-4 xl:grid-cols-[minmax(0,1.45fr)_minmax(360px,0.85fr)]">
        <section className="data-panel">
          <div className="data-panel__header"><div><h2>Distribuição de preços</h2><p>{row.name}</p></div></div>
          <div className="p-4 sm:p-5"><PriceChart products={offers} /></div>
        </section>
        <section className="data-panel">
          <div className="data-panel__header"><div><h2>Ofertas por loja</h2><p>{offers.length} fontes com disponibilidade</p></div></div>
          <div className="divide-y divide-[var(--border)]">
            {offers.map((offer, index) => (
              <a key={`${offer.store}-${offer.url}`} href={offer.url} target="_blank" rel="noreferrer" className="detail-offer-row">
                <span className="store-ranking-index">{String(index + 1).padStart(2, "0")}</span>
                <span className="min-w-0 flex-1"><strong>{offer.store}</strong><small>{formatDate(offer.last_update)}</small></span>
                <span className="detail-offer-logistics"><strong>{analyticsShippingLabel(offer)}</strong><small>{analyticsLeadTimeLabel(offer)}</small></span>
                <span className="text-right"><strong className={index === 0 ? "!text-[var(--success)]" : ""}>{formatCurrency(offer.current_price)}</strong><small>Ver produto</small></span>
                <ExternalLink size={14} />
              </a>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
};

export interface Product {
  id: number;
  name: string;
  brand: string;
  category: string;
  sku?: string | null;
  current_price: number;
  store: string;
  source?: string | null;
  url: string;
  image_url?: string | null;
  last_update: string; // ISO Date string vinda do backend
  description?: string | null;
}

export interface StoreSearchResult {
  key: string;
  label: string;
  available: boolean;
  message?: string | null;
  product: Product | null;
  products?: Product[];
}

export interface ComparisonRow {
  id: number;
  name: string;
  sku?: string | null;
  store_count: number;
  offer_count: number;
  pma: number;
  cheapest_price: number;
  cheapest_store: string;
  cheapest_url: string;
  difference_value: number;
  difference_percent: number;
  offers: Product[];
}

export interface SearchResponse {
  message?: string;
  results?: Product[];
  stores?: StoreSearchResult[];
  comparison?: ComparisonRow[];
}

export interface SkuStatusItem {
  sku: string;
  in_database: boolean;
  product_count: number;
  offer_count: number;
  store_count: number;
  last_update?: string | null;
}

export interface SkuStatusResponse {
  items: SkuStatusItem[];
}

export interface SkuBatchResult extends SkuStatusItem {
  status: "updated" | "cached" | "error";
  message: string;
  results_count: number;
  stores_count: number;
  comparison_count: number;
}

export interface SkuBatchResponse {
  items: SkuBatchResult[];
}

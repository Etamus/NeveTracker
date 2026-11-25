export interface Product {
  internal_code?: string | null;
  external_id?: string | null;
  reference_price?: number | null;
  id: number;
  name: string;
  brand: string;
  category: string;
  sku?: string | null;
  current_price: number | null;
  store: string;
  source?: string | null;
  discovery_source?: string | null;
  url: string;
  image_url?: string | null;
  last_update: string; // ISO Date string vinda do backend
  description?: string | null;
  subfamily?: string | null;
  sku_category?: string | null;
  cluster?: string | null;
  market_type?: "Cativo" | "Competitivo" | string | null;
  cash_price?: number | null;
  installment_price?: number | null;
  installment_count?: number | null;
  installment_value?: number | null;
  payment_difference?: number | null;
  shipping_cost?: number | null;
  shipping_method?: string | null;
  delivery_min_days?: number | null;
  delivery_max_days?: number | null;
  delivery_days_type?: "business" | "calendar" | string | null;
  handling_min_days?: number | null;
  handling_max_days?: number | null;
  destination_zip?: string | null;
  availability?: string | null;
  fulfillment_type?: "2P" | "3P" | null;
  fulfilled_by?: string | null;
  fulfillment_evidence?: string | null;
  logistics_source?: string | null;
  logistics_captured_at?: string | null;
  is_cached?: boolean;
}

export interface StoreSearchResult {
  key: string;
  label: string;
  available: boolean;
  message?: string | null;
  product: Product | null;
  products?: Product[];
  cadence?: "hourly" | "3x_daily" | string | null;
}

export interface ComparisonRow {
  id: number;
  name: string;
  sku?: string | null;
  description?: string | null;
  subfamily?: string | null;
  sku_category?: string | null;
  cluster?: string | null;
  market_type?: "Cativo" | "Competitivo" | string | null;
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
  source_health?: SourceHealthItem[];
}

export interface SourceHealthItem {
  key: string;
  label: string;
  status: "available" | "cached" | "empty" | string;
  offer_count: number;
  cadence?: string | null;
  message?: string | null;
}

export interface SkuStatusItem {
  sku: string;
  in_database: boolean;
  product_count: number;
  offer_count: number;
  store_count: number;
  last_update?: string | null;
  description?: string | null;
  subfamily?: string | null;
  sku_category?: string | null;
  cluster?: string | null;
  market_type?: "Cativo" | "Competitivo" | string | null;
  ean?: string | null;
  mpn?: string | null;
  model?: string | null;
  brand?: string | null;
}

export interface SkuStatusResponse {
  items: SkuStatusItem[];
  total?: number;
}

export interface SkuBatchResult extends SkuStatusItem {
  status: "updated" | "cached" | "error" | "queued" | "running" | "waiting_access" | "cancelled" | "skipped";
  message: string;
  results_count: number;
  stores_count: number;
  comparison_count: number;
  attempts?: number;
}

export interface SkuBatchResponse {
  items: SkuBatchResult[];
}

export type ExtractionJobStatus =
  | "queued"
  | "running"
  | "waiting_access"
  | "paused"
  | "completed"
  | "completed_with_errors"
  | "cancelled"
  | "failed";

export interface ExtractionJob {
  waiting_access?: Array<{ store: "mercado_livre" | "shopee"; status: string; sku?: string; reason?: string; retry_at?: string }>;
  waiting_skus?: number;
  postal_codes?: string[];
  stores?: string[];
  sellers?: Record<string, string[]>;
  owner_user_id?: number | null;
  api_with_scraping?: boolean;
  id: string;
  kind: "single" | "manual" | "database" | "scheduled" | string;
  status: ExtractionJobStatus;
  scope: "all" | "sites" | "retail" | string;
  mode: "quick" | "precise" | string;
  force_refresh: boolean;
  refresh_if_stale: boolean;
  batch_size: number;
  total_skus: number;
  processed_skus: number;
  visited_skus: number;
  success_count: number;
  cached_count: number;
  error_count: number;
  current_sku?: string | null;
  message?: string | null;
  cancel_requested: boolean;
  progress_percent: number;
  work_progress_percent?: number;
  active_skus?: {sku: string; phase: string; message: string; progress?: {done?: number; total?: number}}[];
  general_skus_done?: number;
  quotes_checked?: number;
  quotes_complete?: number;
  observed_skus?: number;
  observations_saved?: number;
  quotes_out_of_stock?: number;
  quotes_incomplete?: number;
  resources?: {active_requests: number; io_budget: number; peak_requests: number};
  started_rounds?: number;
  store_steps_done?: number;
  store_steps_total?: number;
  batches_total: number;
  batches_completed: number;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at?: string | null;
  items?: SkuBatchResult[];
}

export interface CrawlerControlSettings {
  api_with_scraping: boolean;
  default_mode: "quick" | "precise";
  external_discovery_fallback: boolean;
  playwright_product_fallback: boolean;
  playwright_shipping_fallback: boolean;
  concurrent_requests_per_domain: number;
  request_timeout_seconds: number;
  precise_sku_workers: number;
  quick_sku_workers: number;
  quote_workers: number;
  quick_timeout_seconds: number;
  discovery_stores_per_round: number;
  adaptive_parallelism: boolean;
  hardware_acceleration: boolean;
  reuse_public_browser: boolean;
  browser_failure_cooldown_seconds: number;
  updated_at?: string | null;
}

export interface ProductSourceItem {
  id: number;
  sku: string;
  store: string;
  seller?: string | null;
  url: string;
  match_confidence: number;
  match_method: string;
  active: boolean;
  validation_status: string;
  failure_reason?: string | null;
  last_collected_at?: string | null;
}

export interface ProductSourceResponse {
  items: ProductSourceItem[];
  total: number;
  page: number;
  page_size: number;
  active_count: number;
  invalid_count: number;
}

export interface ActiveExtractionJobResponse {
  job?: ExtractionJob | null;
}

export interface SkuImportError {
  row: number;
  message: string;
}

export interface AuthUser {
  logistics_postal_code?: string;
  id: number;
  username: string;
  avatar_data_url?: string | null;
  role: "developer" | "administrator" | "governor" | "viewer";
  is_admin: boolean;
  is_owner: boolean;
  created_at: string;
  updated_at: string;
  last_login?: string | null;
}

export interface AuthLoginResponse {
  access_token: string;
  token_type: "bearer";
  expires_at: string;
  user: AuthUser;
}

export interface AuthUsersResponse {
  items: AuthUser[];
}

export type IntegrationProviderKey =
  | "bright_data"
  | "apify"
  | "dataforseo"
  | "mercado_livre";

export interface IntegrationProviderSettings {
  provider: IntegrationProviderKey;
  label: string;
  description: string;
  category: "discovery" | "store";
  credential_label: string;
  credential_url: string;
  enabled: boolean;
  configured: boolean;
  has_api_key: boolean;
  api_key_masked?: string | null;
  usage_mode: "fallback" | "always";
  supports_usage_mode: boolean;
  supports_limit: boolean;
  max_results: number;
  quota: {
    limit: number | null;
    used: number;
    remaining: number | null;
    unit: string;
    period: "monthly" | "lifetime" | "unmetered";
    period_key?: string | null;
    hard_locked: boolean;
  };
  zone?: string;
  actor_id?: string;
  login?: string;
}

export interface IntegrationSettingsResponse {
  items: IntegrationProviderSettings[];
}

export interface SkuImportResponse {
  filename: string;
  imported_count: number;
  created_count: number;
  updated_count: number;
  skipped_count: number;
  errors: SkuImportError[];
  skus: string[];
  items: SkuStatusItem[];
}

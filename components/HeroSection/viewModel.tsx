import { useState } from "react";
import type {
  ComparisonRow,
  Product,
  SearchResponse,
  StoreSearchResult,
} from "../../models/product.model";
import { getInterfacePreferences } from "../../src/preferences";

export const getApiBaseUrl = () => {
  const configuredApiUrl =
    import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";
  if (configuredApiUrl.startsWith("/") && !configuredApiUrl.startsWith("//")) {
    return configuredApiUrl.replace(/\/+$/, "");
  }
  const apiUrlWithProtocol = /^https?:\/\//i.test(configuredApiUrl)
    ? configuredApiUrl
    : `http://${configuredApiUrl}`;

  return apiUrlWithProtocol.replace(/\/+$/, "");
};

const useHeroSection = () => {
  const [productName, setProductName] = useState("");
  const [products, setProducts] = useState<Product[]>([]);
  const [storeResults, setStoreResults] = useState<StoreSearchResult[]>([]);
  const [comparisonRows, setComparisonRows] = useState<ComparisonRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [lastQuery, setLastQuery] = useState("");

  const handleSearch = async (selectedSkus: string[] = [], queryLabel?: string) => {
    if (loading) return;

    const query = productName.trim();
    if (!query && !selectedSkus.length) return;

    setLoading(true);
    setFeedback("");
    setLastQuery(queryLabel || query);

    const apiBase = getApiBaseUrl();
    const skus = [...new Set(selectedSkus.map((sku) => sku.trim().toUpperCase()).filter(Boolean))];

    try {
      const response = skus.length
        ? await fetch(`${apiBase}/products/search/batch`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              skus,
              force_refresh: false,
              refresh_if_stale: false,
              local_only: true,
              postal_code: getInterfacePreferences().logisticsPostalCode || null,
              scope: "all",
            }),
          })
        : await fetch(`${apiBase}/products/search?q=${encodeURIComponent(query)}`);
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      const data = (await response.json()) as SearchResponse;
      const receivedProducts = data.results || [];
      const receivedStores = data.stores || [];
      const receivedComparison = data.comparison || [];

      setProducts(receivedProducts);
      setStoreResults(receivedStores);
      setComparisonRows(receivedComparison);
      setFeedback(data.message || "");
    } catch (e) {
      console.error("Erro ao buscar dados:", e);
      setProducts([]);
      setStoreResults([]);
      setComparisonRows([]);
      setFeedback("Não foi possível buscar os preços agora.");
    } finally {
      setLoading(false);
    }
  };

  return {
    productName,
    products,
    storeResults,
    comparisonRows,
    loading,
    feedback,
    lastQuery,
    setProductName,
    handleSearch,
  };
};

export default useHeroSection;

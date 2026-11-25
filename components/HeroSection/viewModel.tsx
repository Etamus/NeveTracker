import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ComparisonRow,
  Product,
  SearchResponse,
} from "../../models/product.model";
import { LOCAL_DATA_CHANGED } from "../../src/localData";
import { apiAuthHeaders } from "../../src/apiAuth";

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

const useHeroSection = (postalCodes: string[] = ['01001000']) => {
  const [productName, setProductName] = useState("");
  const [products, setProducts] = useState<Product[]>([]);
  const [comparisonRows, setComparisonRows] = useState<ComparisonRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState("");
  const [lastQuery, setLastQuery] = useState("");
  const [searchCleared, setSearchCleared] = useState(false);
  const request = useRef<AbortController | null>(null);
  const previousInput = useRef("");
  const clearSearch = useCallback(() => {
    request.current?.abort();
    request.current = null;
    setProducts([]); setComparisonRows([]);
    setLastQuery(""); setSearchError(""); setLoading(false);
  }, []);
  useEffect(() => {
    if (!productName.trim()) {
      if (previousInput.current.trim()) setSearchCleared(true);
      clearSearch();
    } else setSearchCleared(false);
    previousInput.current = productName;
  }, [productName, clearSearch]);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(()=>{
    const update=(event:Event)=>{const sku=(event as CustomEvent<{sku:string}>).detail?.sku;setProducts(rows=>rows.filter(row=>row.sku!==sku));setComparisonRows([]);};
    window.addEventListener(LOCAL_DATA_CHANGED,update);return()=>window.removeEventListener(LOCAL_DATA_CHANGED,update);
  },[]);

  const handleSearch = useCallback(async (selectedSkus: string[] = [], queryLabel?: string) => {
    if (loading) return;

    const query = productName.trim();
    if (!query && !selectedSkus.length) return;

    setLoading(true);
    setSearchCleared(false);
    setSearchError("");
    setLastQuery(queryLabel || query);

    const apiBase = getApiBaseUrl();
    const skus = [...new Set(selectedSkus.map((sku) => sku.trim().toUpperCase()).filter(Boolean))];
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;

    try {
      const response = skus.length
        ? await fetch(`${apiBase}/products/search/batch?include_stores=false`, {
            signal: controller.signal,
            method: "POST",
            headers: apiAuthHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({
              skus,
              force_refresh: false,
              refresh_if_stale: false,
              local_only: true,
              postal_code: postalCodes[0] || '01001000',
              postal_codes: postalCodes,
              scope: "all",
            }),
          })
        : await fetch(`${apiBase}/products/search?${new URLSearchParams([['q',query],['include_stores','false'],...postalCodes.map(code=>['postal_codes',code])])}`, { headers: apiAuthHeaders(), signal: controller.signal });
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(response.status < 500 && typeof detail?.detail === "string"
          ? detail.detail : "Não foi possível carregar as ofertas. Tente novamente.");
      }

      const data = (await response.json()) as SearchResponse;
      if (controller.signal.aborted || request.current !== controller) return;
      const receivedProducts = data.results || [];
      const receivedComparison = data.comparison || [];

      setProducts(receivedProducts);
      setComparisonRows(receivedComparison);
    } catch (e) {
      if (controller.signal.aborted || request.current !== controller) return;
      console.error("Erro ao buscar dados:", e);
      setProducts([]);
      setComparisonRows([]);
      setSearchError(e instanceof Error && !(e instanceof TypeError) && !(e instanceof SyntaxError)
        ? e.message : "Não foi possível carregar as ofertas. Verifique a conexão e tente novamente.");
    } finally {
      if (request.current === controller) { request.current = null; setLoading(false); }
    }
  }, [loading, productName, postalCodes]);

  return {
    productName,
    products,
    comparisonRows,
    loading,
    searchError,
    lastQuery,
    searchCleared,
    setProductName,
    handleSearch,
  };
};

export default useHeroSection;

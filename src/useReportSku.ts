import { useEffect, useMemo, useState } from "react";
import { navbarSkus } from "./skuSelection";

export function useReportSku(query: string, selectedSkus: string[]) {
  const candidates = useMemo(() => navbarSkus(query, selectedSkus), [query, selectedSkus]);
  const [choice, setChoice] = useState("");
  const sku = candidates.includes(choice) ? choice : candidates[0] || "";
  const [settledSku, setSettledSku] = useState(sku);
  const typing = selectedSkus.length === 0 && sku !== settledSku && Boolean(sku) && choice !== sku;

  useEffect(() => {
    if (!typing) {
      setSettledSku(sku);
      return;
    }
    const timer = window.setTimeout(() => setSettledSku(sku), 300);
    return () => window.clearTimeout(timer);
  }, [sku, typing]);

  return { candidates, sku, setSku: setChoice, lookupSku: typing ? "" : sku };
}

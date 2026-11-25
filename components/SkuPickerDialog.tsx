import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Search, X } from "lucide-react";
import type { SkuStatusResponse } from "../models/product.model";
import { apiAuthHeaders } from "../src/apiAuth";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import DialogBackdrop from "./DialogBackdrop";
import OrbitingSpheres from "./OrbitingSpheres";

const PAGE_SIZE = 25;
const BULK_PAGE_SIZE = 100;
const LOAD_ERROR = "Não foi possível carregar os SKUs do banco.";
const dateFormatter = new Intl.DateTimeFormat("pt-BR", { dateStyle: "short", timeStyle: "short" });

function formatUpdatedAt(value?: string | null) {
  if (!value) return "Nunca";
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? "Nunca" : dateFormatter.format(date);
}

async function loadSkuPage(filter: string, page: number, pageSize: number, signal: AbortSignal) {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize), q: filter });
  const response = await fetch(`${getApiBaseUrl()}/products/skus/status?${params}`, {
    signal, headers: apiAuthHeaders(),
  });
  if (!response.ok) throw new Error(LOAD_ERROR);
  return await response.json() as SkuStatusResponse;
}

async function loadSkuKeys(filter: string, total: number, signal: AbortSignal) {
  const params = new URLSearchParams({ q: filter });
  const response = await fetch(`${getApiBaseUrl()}/products/skus/keys?${params}`, {
    signal, headers: apiAuthHeaders(),
  });
  if (response.ok) {
    const { skus } = await response.json() as { skus: string[] };
    if (!Array.isArray(skus)) throw new Error(LOAD_ERROR);
    return skus;
  }
  if (response.status !== 404 && response.status !== 405) throw new Error(LOAD_ERROR);

  // Older backends expose the material list only through the status route.
  const firstPage = await loadSkuPage(filter, 1, BULK_PAGE_SIZE, signal);
  const pageCount = Math.ceil((firstPage.total ?? total) / BULK_PAGE_SIZE);
  const skus = firstPage.items.map(item => item.sku);
  for (let page = 2; page <= pageCount; page++) {
    const { items } = await loadSkuPage(filter, page, BULK_PAGE_SIZE, signal);
    skus.push(...items.map(item => item.sku));
  }
  return skus;
}

export default function SkuPickerDialog({ initialSelection, action, actionBusy = false, allowEmpty = false, onApply, onDismiss }: {
  initialSelection: string[];
  action: string;
  actionBusy?: boolean;
  allowEmpty?: boolean;
  onApply: (skus: string[]) => void;
  onDismiss: () => void;
}) {
  const [selected, setSelected] = useState(() => new Set(initialSelection));
  const [filter, setFilter] = useState("");
  const [page, setPage] = useState(1);
  const [result, setResult] = useState<SkuStatusResponse | null>(null);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [bulkError, setBulkError] = useState("");
  const [revision, setRevision] = useState(0);
  const [selectingAll, setSelectingAll] = useState(false);
  const bulkRequest = useRef<AbortController | null>(null);
  const list = useRef<HTMLDivElement>(null);
  const nextBatch = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    const load = async () => {
      try {
        const data = await loadSkuPage(filter, page, PAGE_SIZE, controller.signal);
        if (controller.signal.aborted) return;
        const total = data.total ?? data.items.length;
        setTotal(total);
        setResult(current => page === 1 ? data : {
          ...data,
          items: [...new Map([...(current?.items ?? []), ...data.items].map(item => [item.sku, item])).values()],
        });
      } catch {
        if (!controller.signal.aborted) setError(LOAD_ERROR);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    const timer = filter && page === 1 ? window.setTimeout(() => void load(), 200) : null;
    if (timer === null) void load();
    return () => {
      if (timer !== null) window.clearTimeout(timer);
      controller.abort();
    };
  }, [filter, page, revision]);

  useEffect(() => () => bulkRequest.current?.abort(), []);

  const hasMore = page * PAGE_SIZE < total;
  useEffect(() => {
    const target = nextBatch.current;
    if (!target || !list.current || !hasMore || loading || error) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (!entry.isIntersecting) return;
      observer.disconnect();
      setLoading(true);
      setPage(current => current + 1);
    }, { root: list.current, rootMargin: "0px 0px 64px" });
    observer.observe(target);
    return () => observer.disconnect();
  }, [hasMore, loading, error, page]);

  function toggle(sku: string) {
    setSelected(current => {
      const next = new Set(current);
      if (next.has(sku)) next.delete(sku);
      else next.add(sku);
      return next;
    });
  }

  async function toggleAll() {
    setBulkError("");
    if (selected.size) {
      setSelected(new Set());
      return;
    }
    if (!result || !total || selectingAll) return;
    if (total === result.items.length) {
      setSelected(new Set(result.items.map(item => item.sku)));
      return;
    }
    const controller = new AbortController();
    bulkRequest.current = controller;
    setSelectingAll(true);
    try {
      const skus = await loadSkuKeys(filter, total, controller.signal);
      if (!controller.signal.aborted) setSelected(new Set(skus));
    } catch {
      if (!controller.signal.aborted) setBulkError("Não foi possível marcar todos os SKUs. Tente novamente.");
    } finally {
      if (!controller.signal.aborted) {
        bulkRequest.current = null;
        setSelectingAll(false);
      }
    }
  }

  return createPortal(
    <DialogBackdrop aria-label="Selecionar SKUs" onDismiss={onDismiss}>
      <section className="workspace-dialog workspace-dialog--sku-selector">
        <div className="dialog-header">
          <h2>Selecionar SKUs</h2>
          <button type="button" className="icon-only-button icon-button" onClick={onDismiss} aria-label="Fechar"><X size={17} /></button>
        </div>
        <div className="sku-selector-toolbar">
          <Search size={16} />
          <input type="search" aria-label="Filtrar SKUs" placeholder="Buscar" maxLength={250} value={filter} data-dialog-autofocus
            onChange={(event) => {
              bulkRequest.current?.abort();
              bulkRequest.current = null;
              setSelectingAll(false);
              setBulkError("");
              setError("");
              setResult(null);
              setTotal(0);
              setLoading(true);
              setPage(1);
              setFilter(event.target.value);
              if (list.current) list.current.scrollTop = 0;
            }} />
        </div>
        <div className="sku-selector-results">
          <div className="sku-selector-list" ref={list} aria-busy={loading}>
            {result?.items.length ? result.items.map(item => (
                <label key={item.sku} className="sku-selector-row">
                  <input type="checkbox" checked={selected.has(item.sku)} disabled={selectingAll} onChange={() => toggle(item.sku)} />
                  <span className="min-w-0 flex-1"><strong>{item.sku}</strong><small>{item.offer_count} ofertas · {item.store_count} lojas · {formatUpdatedAt(item.last_update)}</small></span>
                </label>
              )) : !loading && !error && <p className="sku-status-empty">Nenhum SKU encontrado.</p>}
            {error && <div className="search-failure"><p role="alert">{error}</p><button type="button" className="toolbar-button" onClick={() => { setError(""); setLoading(true); setRevision(value => value + 1); }}>Tentar novamente</button></div>}
            {loading && <div className={result ? "sku-selector-loading sku-selector-loading--more" : "sku-selector-loading"} role="status" aria-label="Carregando SKUs"><OrbitingSpheres size={22} /></div>}
            {hasMore && <div ref={nextBatch} className="sku-selector-next-batch" aria-hidden="true" />}
          </div>
        </div>
        {bulkError && <p className="auth-error" role="alert">{bulkError}</p>}
        <div className="sku-selector-footer">
          <span>{selected.size} selecionados</span>
          <div className="sku-selector-footer__actions">
            <button type="button" className="button-secondary" onClick={() => void toggleAll()} disabled={selectingAll || (!selected.size && (!result || !total))}>
              {selectingAll && <OrbitingSpheres size={15} />}{selected.size ? "Desmarcar tudo" : "Marcar tudo"}
            </button>
            <button type="button" className="button-primary" onClick={() => onApply([...selected])} disabled={actionBusy || selectingAll || (!allowEmpty && !selected.size)}>
              {actionBusy && <OrbitingSpheres size={15} />}{action}
            </button>
          </div>
        </div>
      </section>
    </DialogBackdrop>, document.body,
  );
}

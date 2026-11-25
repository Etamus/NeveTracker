import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Plus } from "lucide-react";
import { StyledSelect } from "./StyledSelect";
import { useAnchoredPopover } from "./useAnchoredPopover";
import { EXTRACTION_STORES } from "./ExtractionStoreSelect";
import OrbitingSpheres from "./OrbitingSpheres";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { apiAuthHeaders } from "../src/apiAuth";

export type SellerSelection = Record<string, string[]>;
const nameKey = (value: string) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").trim().toLocaleLowerCase();

export default function ExtractionSellerSelect({ stores, values, onChange }: {
  stores: string[]; values: SellerSelection; onChange: (values: SellerSelection) => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useAnchoredPopover(open, trigger, menu);
  const [store, setStore] = useState("");
  const [options, setOptions] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [text, setText] = useState("");
  const choices = useMemo(() => EXTRACTION_STORES.filter(([key]) => !stores.length || stores.includes(key)).map(([value,label]) => ({value,label})), [stores]);
  const selectedStore = choices.some((choice) => choice.value === store) ? store : choices[0]?.value || "";
  const count = choices.reduce((total, choice) => total + (values[choice.value]?.length || 0), 0);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setError("");
    const timer = window.setTimeout(() => { controller.abort(); setLoading(false); setError("Preencha um nome para adicionar um lojista."); }, 15_000);
    fetch(`${getApiBaseUrl()}/products/extraction-sellers`, {headers:apiAuthHeaders(),signal:controller.signal})
      .then(async (response) => { if (!response.ok) throw new Error("Não foi possível carregar os lojistas salvos. Você pode adicionar um nome."); return response.json() as Promise<{items:{store:string;sellers:string[]}[]}>; })
      .then((data) => { if (!controller.signal.aborted) setOptions(Object.fromEntries(data.items.map((item) => [item.store,item.sellers]))); })
      .catch((reason:unknown) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Falha ao carregar lojistas."); })
      .finally(() => { window.clearTimeout(timer); if (!controller.signal.aborted) setLoading(false); });
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [open]);
  const names = [...new Map([...(options[selectedStore] || []), ...(values[selectedStore] || [])].map((name) => [nameKey(name),name])).values()];
  const selected = values[selectedStore] || [];
  const change = (names: string[]) => {
    const next = {...values};
    if (names.length) next[selectedStore] = names; else delete next[selectedStore];
    onChange(next);
  };
  const add = () => {
    const name = text.trim();
    if (!name || name.length > 160) return;
    if (!selected.some((item) => nameKey(item) === nameKey(name))) change([...selected,name]);
    setText("");
  };
  return <div className="styled-select extraction-seller-select" ref={root} onKeyDown={event => {
    if (open && event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); setOpen(false);
      trigger.current?.focus();
    }
  }}>
    <button ref={trigger} type="button" className="styled-select__trigger" aria-label="Lojistas da extração" aria-expanded={open} onClick={() => setOpen(!open)}><span>{count ? `${count} lojistas selecionados` : "Todos os lojistas"}</span><ChevronDown size={15}/></button>
    {open && <div ref={menu} popover="manual" className="filter-popover extraction-seller-options">
      <StyledSelect ariaLabel="Loja dos lojistas" value={selectedStore} options={choices} onChange={(value) => { setStore(value); setText(""); }}/>
      <div className="extraction-seller-custom"><input aria-label="Nome do lojista" placeholder="Pesquisar ou adicionar lojista..." value={text} maxLength={160} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); add(); } }}/><button type="button" className="icon-only-button icon-button" data-tooltip="Adicionar lojista" aria-label="Adicionar lojista" disabled={!text.trim()} onClick={add}><Plus size={17}/></button></div>
      <button type="button" className="filter-option" onClick={() => change([])}>Todos os lojistas</button>
      {loading && <div className="extraction-seller-loading" role="status" aria-label="Carregando lojistas"><OrbitingSpheres size={18}/></div>}
      {error && <p className="extraction-seller-error" role="alert">{error}</p>}
      <div className="extraction-seller-list">{names.filter((name) => !text || nameKey(name).includes(nameKey(text))).map((name) => <button type="button" className="filter-option" key={nameKey(name)} role="checkbox" aria-checked={selected.includes(name)} onClick={() => change(selected.includes(name) ? selected.filter((item) => item !== name) : [...selected,name])}><span className={`filter-checkbox ${selected.includes(name) ? "filter-checkbox--active" : ""}`}>{selected.includes(name) && <Check size={11}/>}</span><span>{name}</span></button>)}</div>
    </div>}
  </div>;
}

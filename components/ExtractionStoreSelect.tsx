import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import { useAnchoredPopover } from './useAnchoredPopover';
export const EXTRACTION_STORES=[['electrolux','Electrolux'],['midea','Midea'],['consul','Consul'],['brastemp','Brastemp'],['mercado_livre','Mercado Livre'],['magalu','Magazine Luiza'],['amazon','Amazon Brasil'],['leroy_merlin','Leroy Merlin'],['shopee','Shopee'],['dufrio','Dufrio'],['friolar','Friolar'],['refrigeracao_mota','Refrigeração Mota'],['mg_parts','MG Parts'],['gold_service','Gold Service'],['comclick','ComClick']];
export default function ExtractionStoreSelect({ values, onChange }: { values: string[]; onChange: (values: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  useAnchoredPopover(open, trigger, menu);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    document.addEventListener('pointerdown', close);
    return () => document.removeEventListener('pointerdown', close);
  }, [open]);
  return <div ref={root} className="styled-select extraction-store-select" onKeyDown={event => {
    if (open && event.key === 'Escape') {
      event.preventDefault(); event.stopPropagation(); setOpen(false); trigger.current?.focus();
    }
  }}>
    <button ref={trigger} type="button" className="styled-select__trigger" aria-label="Lojas da extração" aria-expanded={open}
      onClick={() => setOpen(!open)}>{values.length ? `${values.length} lojas selecionadas` : 'Todas as lojas'}<ChevronDown size={15}/></button>
    {open && <div ref={menu} popover="manual" className="filter-popover extraction-store-options">
      <button className="filter-option" type="button" onClick={() => onChange([])}>Todas as lojas</button>
      {EXTRACTION_STORES.map(([key, label]) => {
        const selected = values.includes(key);
        return <button type="button" key={key} className="filter-option" role="checkbox" aria-checked={selected}
          onClick={() => onChange(selected ? values.filter(value => value !== key) : [...values, key])}>
          <span className={`filter-checkbox${selected ? ' filter-checkbox--active' : ''}`}>{selected && <Check size={11}/>}</span><span>{label}</span>
        </button>;
      })}
    </div>}
  </div>;
}

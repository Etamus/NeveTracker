type DeliveryRange = {
  delivery_min_days?: number | null;
  delivery_max_days?: number | null;
  delivery_days_type?: string | null;
};

export function formatDeliveryRange(quote: DeliveryRange, missing = "Não informado") {
  const { delivery_min_days: min, delivery_max_days: max, delivery_days_type: type } = quote;
  if (min == null && max == null) return missing;
  const singleDay = (min ?? max) === 1 && (max ?? min) === 1;
  const day = singleDay ? "dia" : "dias";
  const unit = type === "business" ? (singleDay ? "dia útil" : "dias úteis") :
    type === "calendar" ? (singleDay ? "dia corrido" : "dias corridos") : `${day} (tipo não informado)`;
  if (min == null) return `Até ${max} ${unit}`;
  if (max == null) return `A partir de ${min} ${unit}`;
  return `${min === max ? min : `${min}–${max}`} ${unit}`;
}

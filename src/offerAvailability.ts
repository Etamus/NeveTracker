import type { Product } from "../models/product.model";
import { STORE_OPTIONS } from "./preferences";

const normalizeStore = (value: string) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
const aliases = new Map(STORE_OPTIONS.flatMap(({ label, aliases }) =>
  [label, ...aliases].map((alias) => [normalizeStore(alias), label] as const)));

export const canonicalStoreLabel = (value: string) => aliases.get(normalizeStore(value));

export function offerIsAvailable(offer: Pick<Product, "availability">): boolean {
  const status = (offer.availability || "").split("/").pop()?.toLowerCase() || "";
  return !["outofstock", "out_of_stock", "out of stock", "discontinued", "soldout", "sold_out", "sem estoque", "esgotado", "indisponivel", "indisponível"].includes(status);
}

export function offerSellerKey(offer: Pick<Product, "source" | "store">): string {
  const source = offer.source || offer.store;
  return `${normalizeStore(canonicalStoreLabel(source) || source)}|${normalizeStore(offer.store)}`;
}

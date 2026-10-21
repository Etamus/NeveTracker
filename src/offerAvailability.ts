import type { Product } from "../models/product.model";
import { STORE_OPTIONS } from "./preferences";

export function offerIsAvailable(offer: Pick<Product, "availability">): boolean {
  const status = (offer.availability || "").split("/").pop()?.toLowerCase() || "";
  return !["outofstock", "out_of_stock", "out of stock", "discontinued", "soldout", "sold_out", "sem estoque", "esgotado", "indisponivel", "indisponível"].includes(status);
}

export function offerSellerKey(offer: Pick<Product, "source" | "store">): string {
  const normalize = (value: string) => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().replace(/\s+/g, " ").trim();
  const source = normalize(offer.source || offer.store);
  const storefront = STORE_OPTIONS.find((store) => store.aliases.some((alias) => normalize(alias) === source));
  return `${normalize(storefront?.label || source)}|${normalize(offer.store)}`;
}

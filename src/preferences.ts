export const INTERFACE_PREFERENCES_KEY = "nevetrack-interface-preferences";
export const INTERFACE_PREFERENCES_EVENT = "nevetrack-interface-preferences-updated";
const PREVIOUS_INTERFACE_PREFERENCES_KEY = `${["neve", "price"].join("")}-interface-preferences`;

export const STORE_OPTIONS = [
  { label: "Mercado Livre", aliases: ["mercado livre", "meli", "-ml", "mlb"] },
  { label: "Magazine Luiza", aliases: ["magazine luiza", "magalu"] },
  { label: "Amazon Brasil", aliases: ["amazon brasil", "amazon"] },
  { label: "Leroy Merlin", aliases: ["leroy merlin", "leroy"] },
  { label: "Shopee", aliases: ["shopee"] },
  { label: "Duofrio", aliases: ["duofrio", "dufrio"] },
  { label: "Friolar", aliases: ["friolar", "friolar pecas", "friolar peças"] },
  { label: "Refrigeração Mota", aliases: ["refrigeração mota", "refrigeracao mota"] },
  { label: "MG Parts", aliases: ["mg parts", "mgparts"] },
  { label: "Gold Service", aliases: ["gold service", "goldservice"] },
  { label: "ComClick", aliases: ["comclick", "com click"] },
];

export const currencyOptions = [
  { value: "BRL", label: "Real brasileiro" },
  { value: "USD", label: "Dólar americano" },
  { value: "EUR", label: "Euro" },
];

export const localeOptions = [
  { value: "pt-BR", label: "Brasil" },
  { value: "en-US", label: "Estados Unidos" },
  { value: "de-DE", label: "Europa" },
];

export type InterfacePreferences = {
  currency: string;
  locale: string;
  defaultStores: string[];
  logisticsPostalCode: string;
};

export const defaultInterfacePreferences: InterfacePreferences = {
  currency: "BRL",
  locale: "pt-BR",
  defaultStores: STORE_OPTIONS.map((store) => store.label),
  logisticsPostalCode: "",
};

const validCurrencies = new Set(currencyOptions.map((option) => option.value));
const validLocales = new Set(localeOptions.map((option) => option.value));
const validStores = new Set(STORE_OPTIONS.map((store) => store.label));

export const getInterfacePreferences = (): InterfacePreferences => {
  try {
    let storedPreferences = window.localStorage.getItem(INTERFACE_PREFERENCES_KEY);
    if (!storedPreferences) {
      storedPreferences = window.localStorage.getItem(PREVIOUS_INTERFACE_PREFERENCES_KEY);
      if (storedPreferences) {
        window.localStorage.setItem(INTERFACE_PREFERENCES_KEY, storedPreferences);
        window.localStorage.removeItem(PREVIOUS_INTERFACE_PREFERENCES_KEY);
      }
    }
    if (!storedPreferences) return defaultInterfacePreferences;

    const parsed = JSON.parse(storedPreferences) as Partial<InterfacePreferences>;
    const defaultStores = Array.isArray(parsed.defaultStores)
      ? parsed.defaultStores.filter((store) => validStores.has(store))
      : defaultInterfacePreferences.defaultStores;

    return {
      currency: parsed.currency && validCurrencies.has(parsed.currency) ? parsed.currency : "BRL",
      locale: parsed.locale && validLocales.has(parsed.locale) ? parsed.locale : "pt-BR",
      defaultStores: defaultStores.length ? defaultStores : defaultInterfacePreferences.defaultStores,
      logisticsPostalCode:
        typeof parsed.logisticsPostalCode === "string"
          ? parsed.logisticsPostalCode.replace(/\D/g, "").slice(0, 8)
          : "",
    };
  } catch {
    return defaultInterfacePreferences;
  }
};

export const saveInterfacePreferences = (preferences: InterfacePreferences) => {
  window.localStorage.setItem(INTERFACE_PREFERENCES_KEY, JSON.stringify(preferences));
  window.dispatchEvent(new CustomEvent(INTERFACE_PREFERENCES_EVENT, { detail: preferences }));
};

export const createCurrencyFormatter = (
  preferences: InterfacePreferences,
  minimumFractionDigits = 2,
  maximumFractionDigits = 2,
) =>
  new Intl.NumberFormat(preferences.locale, {
    style: "currency",
    currency: preferences.currency,
    minimumFractionDigits,
    maximumFractionDigits,
  });

export const createCompactFormatter = (preferences: InterfacePreferences) =>
  new Intl.NumberFormat(preferences.locale, {
    notation: "compact",
    maximumFractionDigits: 1,
  });

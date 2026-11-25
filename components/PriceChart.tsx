import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useEffect, useMemo, useState } from "react";
import type { Product } from "../models/product.model";
import { offerIsAvailable } from "../src/offerAvailability";
import {
  createCurrencyFormatter,
  getInterfacePreferences,
  INTERFACE_PREFERENCES_EVENT,
  type InterfacePreferences,
} from "../src/preferences";

interface PriceChartProps {
  products: Product[];
}

const shortStore = (value: string) =>
  value.length > 13 ? `${value.slice(0, 12)}…` : value;

export const PriceChart = ({ products }: PriceChartProps) => {
  const [interfacePreferences, setInterfacePreferences] = useState<InterfacePreferences>(getInterfacePreferences);
  const currencyFormatter = useMemo(() => createCurrencyFormatter(interfacePreferences, 0, 0), [interfacePreferences]);
  const preciseCurrencyFormatter = useMemo(() => createCurrencyFormatter(interfacePreferences), [interfacePreferences]);

  useEffect(() => {
    const syncPreferences = () => setInterfacePreferences(getInterfacePreferences());
    window.addEventListener(INTERFACE_PREFERENCES_EVENT, syncPreferences);
    return () => window.removeEventListener(INTERFACE_PREFERENCES_EVENT, syncPreferences);
  }, []);

  const chartData = useMemo(() => {
    const cheapest = new Map<string, number>();
    for (const product of products) {
      const price = product.current_price;
      if (price == null || !Number.isFinite(price) || price <= 0 || !offerIsAvailable(product)) continue;
      if (price < (cheapest.get(product.store) ?? Infinity)) cheapest.set(product.store, price);
    }
    return [...cheapest].map(([store, price]) => ({ store, price })).sort((a, b) => a.price - b.price);
  }, [products]);

  if (!chartData.length) return null;

  return (
    <div className="h-[320px] min-h-[280px] w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={chartData} margin={{ top: 12, right: 14, left: 4, bottom: 18 }}>
          <CartesianGrid stroke="var(--chart-grid)" vertical={false} strokeDasharray="3 4" />
          <XAxis
            dataKey="store"
            axisLine={false}
            tickLine={false}
            tick={{ fill: "var(--muted)", fontSize: 10, fontWeight: 500 }}
            tickFormatter={shortStore}
            tickMargin={12}
            interval="preserveStartEnd"
            minTickGap={28}
          />
          <YAxis
            axisLine={false}
            tickLine={false}
            tick={{ fill: "var(--muted)", fontSize: 10 }}
            tickFormatter={(value) => currencyFormatter.format(Number(value))}
            width={78}
            tickMargin={8}
          />
          <Tooltip
            cursor={{ stroke: "#a6b0bd", strokeDasharray: "3 4" }}
            contentStyle={{
              background: "var(--tooltip-bg)",
              border: "none",
              borderRadius: "7px",
              boxShadow: "0 12px 28px rgba(15, 23, 42, 0.18)",
              color: "#fff",
              fontFamily: "inherit",
              fontSize: "12px",
              padding: "9px 11px",
            }}
            itemStyle={{ color: "#fff", fontWeight: 600 }}
            labelStyle={{ color: "#aeb8c6", marginBottom: "3px" }}
            formatter={(value) => [preciseCurrencyFormatter.format(Number(value)), "Preço"]}
          />
          <Area
            type="monotone"
            dataKey="price"
            name="Preço"
            stroke="var(--accent)"
            strokeWidth={2.25}
            fill="var(--chart-fill)"
            fillOpacity={0.8}
            dot={{ r: 3.5, fill: "var(--surface)", stroke: "var(--accent)", strokeWidth: 2 }}
            activeDot={{ r: 5, fill: "var(--accent)", stroke: "var(--surface)", strokeWidth: 2 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
};

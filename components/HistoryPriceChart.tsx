import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export type HistoryPoint = { date: string; minimum: number | null; average: number | null; maximum: number | null; offers: number };

export default function HistoryPriceChart({ series, money }: { series: HistoryPoint[]; money: Intl.NumberFormat }) {
  const dateLabel = (value: string) => value.split("-").reverse().join("/");
  return <ResponsiveContainer width="100%" height={280}>
    <LineChart data={series}>
      <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" />
      <XAxis dataKey="date" tickFormatter={dateLabel} tick={{ fill: "var(--muted)", fontSize: 10 }} />
      <YAxis tick={{ fill: "var(--muted)", fontSize: 10 }} />
      <Tooltip labelFormatter={(label) => dateLabel(String(label))} formatter={(value) => money.format(Number(value))} itemStyle={{ fontSize: "0.9375em" }} contentStyle={{ background: "var(--surface)", borderColor: "var(--border)", borderRadius: 8 }} />
      <Legend />
      <Line dataKey="minimum" name="Mínimo" stroke="var(--success)" dot={false} />
      <Line dataKey="average" name="Média" stroke="var(--accent)" strokeWidth={2} dot={false} />
      <Line dataKey="maximum" name="Máximo" stroke="var(--muted)" dot={false} />
    </LineChart>
  </ResponsiveContainer>;
}

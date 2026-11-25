import { useEffect, useState } from "react";

export default function ExtractionElapsedTime({ startedAt, endedAt }: { startedAt: number; endedAt?: number }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (endedAt !== undefined) return;
    const interval = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [endedAt]);

  const seconds = Math.max(0, Math.floor(((endedAt ?? now) - startedAt) / 1000));
  const elapsed = Number.isFinite(seconds)
    ? [Math.floor(seconds / 3600), Math.floor((seconds % 3600) / 60), seconds % 60]
        .map((value) => String(value).padStart(2, "0"))
        .join(":")
    : "—";

  return <small className="extraction-job-card__elapsed" role="timer" aria-live="off">Tempo decorrido: {elapsed}</small>;
}

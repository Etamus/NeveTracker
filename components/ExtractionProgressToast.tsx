import { ChevronDown, CircleX, Download, Minus } from "lucide-react";
import { useState } from "react";
import type { ExtractionJob } from "../models/product.model";
import ExtractionElapsedTime from "./ExtractionElapsedTime";
import OrbitingSpheres from "./OrbitingSpheres";

const retryClock = new Intl.DateTimeFormat("pt-BR", { timeStyle: "short", timeZone: "America/Sao_Paulo" });

function retryMessage(value?: string) {
  const retryAt = value ? new Date(value) : null;
  return retryAt && !Number.isNaN(retryAt.getTime())
    ? `próxima tentativa às ${retryClock.format(retryAt)}`
    : "aguardando nova tentativa";
}

export default function ExtractionProgressToast({ job, title, startedAt, currentUserId, canCancel, cancelPending, cancelError, connectionLost, accessBusy, accessMessage, onCancel, onResolveAccess }: {
  job: ExtractionJob; title: string; startedAt: number; currentUserId?: number; canCancel: boolean; connectionLost: boolean;
  cancelPending: boolean; cancelError?: string;
  accessBusy: string | null; accessMessage: string; onCancel: () => void;
  onResolveAccess: (store: "shopee" | "mercado_livre", action: "open" | "confirm") => void;
}) {
  const [minimized, setMinimized] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
  const progress = Math.min(100, Math.max(0, job.mode === "precise"
    ? 100 * (job.store_steps_done ?? 0) / Math.max(1, job.store_steps_total ?? 0) : job.progress_percent));
  const owner = job.owner_user_id === currentUserId;
  if (minimized) return <button type="button" className="icon-only-button extraction-job-mini" onClick={() => setMinimized(false)} aria-label="Restaurar andamento da extração" data-tooltip={`${title} · ${Math.round(progress)}%`}>
    <svg viewBox="0 0 44 44" role="progressbar" aria-label="Progresso da extração" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}>
      <circle cx="22" cy="22" r="19" className="extraction-job-mini__track" />
      <circle cx="22" cy="22" r="19" pathLength="100" strokeDasharray="100" strokeDashoffset={100 - progress} className="extraction-job-mini__value" />
    </svg><Download size={17} aria-hidden="true" />
  </button>;
  return <aside className="extraction-job-card extraction-job-card--active extraction-job-card--floating" role="status" aria-live="polite">
    <div className="extraction-job-card__head">
      <span className="extraction-job-card__icon"><OrbitingSpheres size={20} paused={job.status === "paused" || job.status === "waiting_access"} /></span>
      <span className="extraction-job-card__copy"><strong>{title}</strong><small>{job.mode === "precise" ? `${job.visited_skus} de ${job.total_skus} SKUs iniciados` : `${job.processed_skus} de ${job.total_skus} SKUs concluídos`}</small><ExtractionElapsedTime startedAt={startedAt} /></span>
      <div className="extraction-job-card__actions">
        <button type="button" className="icon-only-button button-quiet" data-tooltip="Minimizar" aria-label="Minimizar andamento da extração" onClick={() => setMinimized(true)}><Minus size={17} /></button>
        {canCancel && !connectionLost && !job.cancel_requested && <button type="button" className="icon-only-button button-quiet" disabled={cancelPending} onClick={onCancel} data-tooltip="Cancelar extração" aria-label="Cancelar extração"><CircleX size={18} /></button>}
      </div>
    </div>
    <div className="extraction-job-progress" role="progressbar" aria-label="Progresso da extração" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress}><span style={{ width: `${progress}%` }} /></div>
    <p className="extraction-job-card__message">{connectionLost ? "Backend indisponível. Tentando reconectar..." :
      job.cancel_requested ? "Cancelamento solicitado. Aguardando o encerramento das tarefas em andamento." :
      cancelPending ? "Solicitando cancelamento..." : job.message || "Preparando coleta"}</p>
    {cancelError && !job.cancel_requested && <p className="auth-error" role="alert">{cancelError}</p>}
    {!job.cancel_requested && !!job.waiting_access?.length && <div className="extraction-job-access">{job.waiting_access.map(access => <div key={access.store}>
      <span>{access.store === "shopee" ? "Shopee" : "Mercado Livre"}{access.sku ? ` · ${access.sku}` : ""}: {access.status === "ready" ? "retomando pendências" : access.status === "retrying" ? retryMessage(access.retry_at) : "a loja solicitou verificação de acesso"}.</span>
      {access.status === "waiting" && owner && <div className="extraction-job-access__actions"><button type="button" className="button-secondary" disabled={accessBusy === access.store} onClick={() => onResolveAccess(access.store, "open")}>Abrir loja</button><button type="button" className="button-secondary" disabled={accessBusy === access.store} onClick={() => onResolveAccess(access.store, "confirm")}>Já corrigi</button></div>}
    </div>)}{accessMessage && <span role="status">{accessMessage}</span>}</div>}
    <button type="button" className="extraction-job-details-toggle" aria-expanded={detailsOpen} onClick={() => setDetailsOpen(open => !open)}>Detalhes<ChevronDown size={15} className={detailsOpen ? "rotate-180" : ""} /></button>
    {detailsOpen && <div className="extraction-job-details">
      {(job.mode !== "precise" || job.error_count > 0) && <div className="extraction-job-card__meta extraction-job-card__counters">
        {job.mode !== "precise" && <span>{job.success_count} concluídos</span>}
        {job.error_count > 0 && <span>{job.error_count} {job.error_count === 1 ? "erro" : "erros"}</span>}
      </div>}
      {!!job.active_skus?.length && <div className="extraction-job-workers">{job.active_skus.map(item => <div key={item.sku}><strong>{item.sku}</strong><span data-tooltip={item.message}>{item.message || "Iniciando coleta"}</span>{!!item.progress?.total && <div className="extraction-worker-progress" role="progressbar" aria-label={`Cotações de ${item.sku}`} aria-valuemin={0} aria-valuemax={item.progress.total} aria-valuenow={item.progress.done ?? 0}><span style={{ width: `${Math.min(100, 100 * (item.progress.done ?? 0) / item.progress.total)}%` }} /></div>}</div>)}</div>}
    </div>}
  </aside>;
}

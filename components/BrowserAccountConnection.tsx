import OrbitingSpheres from "./OrbitingSpheres";
import { useEffect, useState } from "react";
import { ExternalLink } from "lucide-react";
import { getApiBaseUrl } from "./HeroSection/viewModel";
import { apiAuthHeaders } from "../src/apiAuth";

type Store = "mercado_livre" | "shopee";
type AccountStatus = { status: string; message: string; enabled?: boolean; intervention?: { sku: string; reason: string } | null };

export default function BrowserAccountConnection({ store }: { store: Store }) {
  const [account, setAccount] = useState<AccountStatus>({ status: "loading", message: "" });
  const [busy, setBusy] = useState(false);
  const label = store === "shopee" ? "Shopee" : "Mercado Livre";
  useEffect(() => {
    const controller = new AbortController();
    let timer: number | undefined;
    const load = async () => {
      try {
        const response = await fetch(`${getApiBaseUrl()}/integrations/accounts/${store}`, { headers: apiAuthHeaders(), signal: controller.signal });
        const data = await response.json() as AccountStatus;
        if (!response.ok) throw new Error("Não foi possível consultar a conta.");
        if (!controller.signal.aborted) setAccount(data);
      } catch {
        if (!controller.signal.aborted) setAccount((current) => ({ ...current, status: "error", message: "Não foi possível consultar a conta. Tente novamente." }));
      }
      if (!controller.signal.aborted) timer = window.setTimeout(() => void load(), 2000);
    };
    void load();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [store]);
  const action = async (type: "connect" | "confirm") => {
    setBusy(true);
    try {
      const response = await fetch(`${getApiBaseUrl()}/integrations/accounts/${store}/${type}`, { method: "POST", headers: apiAuthHeaders() });
      const data = await response.json() as AccountStatus & { detail?: string };
      if (!response.ok) throw new Error(data.detail || "Não foi possível conectar a conta.");
      setAccount((current) => ({ ...current, ...data }));
    } catch (reason) { setAccount((current) => ({ ...current, status: "error", message: reason instanceof Error ? reason.message : "Falha ao conectar a conta." })); }
    finally { setBusy(false); }
  };
  const toggle = async (enabled: boolean) => {
    setBusy(true);
    try {
      const response = await fetch(`${getApiBaseUrl()}/integrations/accounts/${store}`, { method: "PUT", headers: apiAuthHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ enabled }) });
      const data = await response.json() as AccountStatus & { detail?: string };
      if (!response.ok) throw new Error(data.detail || "Não foi possível alterar o uso da conta.");
      setAccount(data);
    } catch (reason) { setAccount((current) => ({ ...current, message: reason instanceof Error ? reason.message : "Falha ao salvar." })); }
    finally { setBusy(false); }
  };
  const waiting = account.status === "opening" || account.status === "waiting";
  return <div className="integration-provider integration-provider--store integration-provider--browser">
    <div className="integration-provider__head"><div><div className="integration-provider__title"><strong>{label}</strong></div><span>Conta do navegador para coleta de anúncios e cotações.</span></div>
      {account.enabled === undefined ? (
        account.status === "loading" && <span role="status" aria-label="Carregando estado da conta"><OrbitingSpheres size={17} /></span>
      ) : (
        <label className="schedule-toggle">
          <input type="checkbox" aria-label={`Usar conta do ${label}`} checked={account.enabled} disabled={busy} onChange={(event) => void toggle(event.target.checked)} />
          <span className="schedule-toggle__track" aria-hidden="true"><span className="schedule-toggle__thumb" /></span>
        </label>
      )}
    </div>
    <div className="integration-config"><section className="integration-config-section">
    <div className="integration-config-section__head"><strong>Conta para extrações</strong></div>
    <div className="integration-account-actions">
      <button type="button" className="button-secondary" onClick={() => void action("connect")} disabled={busy || waiting || !account.enabled}>
        {account.status === "opening" ? <OrbitingSpheres size={15} className="animate-spin" /> : <ExternalLink size={15} />}
        {account.intervention ? "Abrir loja" : account.status === "saved" || account.status === "connected" ? "Entrar novamente" : "Conectar conta"}
      </button>
      {(waiting || account.intervention) && <button type="button" className="button-primary" onClick={() => void action("confirm")} disabled={busy || !account.enabled || account.status === "opening"}>Já corrigi</button>}
    </div>
    </section></div>
    {account.message && !(store === "shopee" && account.status === "opening") && <p className="integration-browser-status" role="status">{account.message}</p>}
  </div>;
}

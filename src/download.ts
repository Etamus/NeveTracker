export async function downloadExcel(url: string, filename?: string, headers?: HeadersInit, timeoutMs = 180_000, request: Pick<RequestInit, "method" | "body"> = {}) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { ...request, headers, signal: controller.signal });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({})) as { detail?: unknown };
      throw new Error(typeof payload.detail === "string" ? payload.detail : "Não foi possível exportar o relatório.");
    }
    const blob = await response.blob();
    if (!blob.size) throw new Error("O arquivo exportado está vazio. Tente novamente.");
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const suggested = response.headers.get("Content-Disposition")?.match(/filename="?([^";]+)"?/i)?.[1];
    link.href = objectUrl; link.download = filename || suggested || "nevetracker-analytics.xlsx";
    document.body.appendChild(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  } catch (reason) {
    if (controller.signal.aborted) throw new Error("A exportação excedeu o tempo de espera. Tente novamente em instantes.");
    throw reason;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function downloadExcel(url: string, filename: string, headers?: HeadersInit) {
  const response = await fetch(url, { headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({})) as { detail?: unknown };
    throw new Error(typeof payload.detail === "string" ? payload.detail : "Não foi possível exportar o relatório.");
  }
  const objectUrl = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = objectUrl; link.download = filename;
  document.body.appendChild(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

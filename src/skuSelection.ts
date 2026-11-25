export function navbarSkus(query: string, selectedSkus: string[]) {
  return [...new Set(selectedSkus.length ? selectedSkus :
    query.toUpperCase().match(/\bW\d{6,}\b|\bMD-RT\d+[A-Z]+\d*\b|\b[A-Z]{1,8}\d+[A-Z0-9]*(?:-[A-Z0-9]+)*\b|\b\d{6,}\b/g) || [])];
}

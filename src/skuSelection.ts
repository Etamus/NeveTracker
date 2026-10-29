export function navbarSkus(query: string, selectedSkus: string[]) {
  return [...new Set(selectedSkus.length ? selectedSkus :
    query.toUpperCase().match(/\bW\d{6,}\b|\bMD-RT\d+[A-Z]+\d*\b|\b(?:BWF|BRO|CRD|CRM|BRM|BWJ|LEE|IM|IF|CIX)\d+[A-Z]*\b|\b\d{6,}\b/g) || [])];
}

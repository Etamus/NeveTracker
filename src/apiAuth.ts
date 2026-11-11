export function apiAuthHeaders(initial?: HeadersInit): Headers {
  const headers = new Headers(initial);
  const token = window.localStorage.getItem('nevetracker-auth-token');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return headers;
}

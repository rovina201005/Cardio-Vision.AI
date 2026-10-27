
export const API_BASE_URL = (
  import.meta.env.VITE_API_URL ||
  'https://cardiovision-api-kkzr.onrender.com'
).replace(/\/$/, '')

export interface HealthResponse {
  status: string
  model_status: string
  message: string
}

export async function apiFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const token = localStorage.getItem('cardiovision_token')
  const headers = new Headers(init.headers)

  if (token) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  return fetch(input, {
    ...init,
    headers,
  })
}

export async function checkBackendHealth(): Promise<HealthResponse> {
  const response = await fetch(`${API_BASE_URL}/api/health`)

  if (!response.ok) {
    throw new Error('Backend health check failed')
  }

  return response.json()
}

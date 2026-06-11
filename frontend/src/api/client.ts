const BASE = ''  // same origin

interface RequestOptions {
  signal?: AbortSignal
}

async function request<T>(method: string, path: string, body?: unknown, options?: RequestOptions): Promise<T> {
  const res = await requestRaw(method, path, body, options)
  return res.json()
}

async function requestRaw(method: string, path: string, body?: unknown, options?: RequestOptions): Promise<Response> {
  const url = `${BASE}${path}`
  console.log(`[API] ${method} ${url}`)
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
    signal: options?.signal,
  })
  if (!res.ok) {
    const text = await res.text()
    console.error(`[API ERROR] ${method} ${url} - ${res.status}: ${text}`)
    throw new Error(`${res.status} ${text}`)
  }
  return res
}

async function requestText(method: string, path: string, body?: unknown, options?: RequestOptions): Promise<string> {
  const res = await requestRaw(method, path, body, options)
  return res.text()
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) => request<T>('GET', path, undefined, options),
  put: <T>(path: string, body: unknown) => request<T>('PUT', path, body),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  delete: <T>(path: string) => request<T>('DELETE', path),
  getText: (path: string, options?: RequestOptions) => requestText('GET', path, undefined, options),
  postText: (path: string, body?: unknown) => requestText('POST', path, body),
}

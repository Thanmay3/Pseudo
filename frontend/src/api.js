export async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    credentials: options.credentials ?? 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Pseudo-Request': '1', ...options.headers },
    signal: AbortSignal.timeout(20000),
  });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(typeof data.detail === 'string' ? data.detail : 'Please check your input and try again.');
    error.status = response.status;
    throw error;
  }
  return data;
}

export function post(path, body) {
  return request(path, { method: 'POST', body: JSON.stringify(body) });
}

const DEFAULT_TIMEOUT_MS = 90000;
const GET_NETWORK_RETRIES = 2;

const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));

function friendlyNetworkError(error){
  const raw=String(error?.message||error||'').trim();
  if(error?.name==='AbortError')return null;
  if(error instanceof TypeError || /failed to fetch|networkerror|load failed/i.test(raw)){
    return new Error('SolarGrid API is temporarily unreachable. Keep the server terminal open and wait a moment, then retry.');
  }
  return error;
}

export async function api(url, options={}) {
  const timeoutMs = Number(options.timeoutMs ?? DEFAULT_TIMEOUT_MS);
  const {timeoutMs: _ignored, retryNetwork, ...requestOptions} = options;
  const method=String(requestOptions.method||'GET').toUpperCase();
  // Automatic network retries are GET-only. Replaying POST/Execute could create
  // a duplicate operational action, so mutation calls are never auto-replayed.
  const maxRetries=retryNetwork===false?0:(method==='GET'?GET_NETWORK_RETRIES:0);

  let attempt=0;
  while(true){
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const init = {
      ...requestOptions,
      signal: requestOptions.signal || controller.signal,
      cache: requestOptions.cache || 'no-store',
      credentials: requestOptions.credentials || 'same-origin',
      headers: {'Content-Type':'application/json', ...(requestOptions.headers||{})},
    };

    try {
      const response = await fetch(url, init);
      const text = await response.text();
      let data = {};
      if (text) {
        try {
          data = JSON.parse(text);
        } catch {
          throw new Error(`Server returned an invalid JSON response (HTTP ${response.status}).`);
        }
      }

      if (!response.ok) {
        const details = Array.isArray(data.errors) && data.errors.length ? ` (${data.errors.join('; ')})` : '';
        throw new Error((data.message || data.error || `Request failed (${response.status})`) + details);
      }
      return data;
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new Error(`Request timed out after ${Math.round(timeoutMs/1000)} seconds.`);
      }
      const networkError=friendlyNetworkError(error);
      const isNetwork=networkError!==error;
      if(isNetwork && attempt<maxRetries){
        attempt+=1;
        await sleep(400*attempt);
        continue;
      }
      throw networkError;
    } finally {
      clearTimeout(timer);
    }
  }
}

export const post=(url,body={},options={})=>api(url,{...options,method:'POST',body:JSON.stringify(body)});

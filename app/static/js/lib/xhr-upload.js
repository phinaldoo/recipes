export class UploadRequestError extends Error {
  constructor(message, status = 0, payload = null, code = "request") {
    super(message);
    this.name = "UploadRequestError";
    this.status = status;
    this.payload = payload;
    this.code = code;
  }
}

function parsePayload(xhr) {
  const contentType = xhr.getResponseHeader?.("content-type") || "";
  if (!contentType.includes("json")) return xhr.responseText || "";
  try {
    return JSON.parse(xhr.responseText || "null");
  } catch {
    return null;
  }
}

export function uploadFormData(path, body, options = {}) {
  const {
    csrfToken = "",
    signal,
    onUploadProgress = () => {},
    onUploadComplete = () => {},
    fallbackMessage = "The request failed.",
    networkMessage = fallbackMessage,
    cancelledMessage = fallbackMessage,
    xhrFactory = () => new XMLHttpRequest(),
  } = options;

  return new Promise((resolve, reject) => {
    const xhr = xhrFactory();
    let settled = false;

    const finish = (callback) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener("abort", abort);
      callback();
    };
    const abort = () => xhr.abort();

    xhr.open("POST", path);
    xhr.withCredentials = true;
    xhr.setRequestHeader("Accept", "application/json");
    if (csrfToken) xhr.setRequestHeader("X-CSRF-Token", csrfToken);

    xhr.upload.addEventListener("progress", (event) => {
      const percent = event.lengthComputable && event.total > 0
        ? Math.min(100, Math.round((event.loaded / event.total) * 100))
        : null;
      onUploadProgress({ loaded: event.loaded, total: event.total, percent });
    });
    xhr.upload.addEventListener("load", onUploadComplete);
    xhr.addEventListener("load", () => {
      const payload = parsePayload(xhr);
      if (xhr.status >= 200 && xhr.status < 300) {
        finish(() => resolve(payload));
        return;
      }
      const message = payload?.error?.message || payload?.detail || fallbackMessage;
      finish(() => reject(new UploadRequestError(message, xhr.status, payload)));
    });
    xhr.addEventListener("error", () => {
      finish(() => reject(new UploadRequestError(networkMessage, 0, null, "network")));
    });
    xhr.addEventListener("abort", () => {
      finish(() => reject(new UploadRequestError(cancelledMessage, 0, null, "cancelled")));
    });

    if (signal?.aborted) {
      finish(() => reject(new UploadRequestError(cancelledMessage, 0, null, "cancelled")));
      return;
    }
    signal?.addEventListener("abort", abort, { once: true });
    xhr.send(body);
  });
}

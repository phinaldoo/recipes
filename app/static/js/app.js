import { t } from "./i18n.js";

const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const method = (options.method || "GET").toUpperCase();
  if (!(options.body instanceof FormData) && options.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) headers.set("X-CSRF-Token", csrfToken);
  headers.set("Accept", "application/json");
  const response = await fetch(path, { ...options, method, headers, credentials: "same-origin" });
  if (response.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.assign(`/login?reason=expired&next=${next}`);
    throw new ApiError(t("login.expired"), 401, null);
  }
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = payload?.error?.message || payload?.detail || t("error.generic");
    throw new ApiError(message, response.status, payload);
  }
  return payload;
}

export function toast(message, type = "success") {
  const region = document.querySelector("[data-toast-region]");
  if (!region) return;
  const item = document.createElement("div");
  item.className = `toast toast--${type}`;
  item.textContent = message;
  region.append(item);
  window.setTimeout(() => item.remove(), 4200);
}

export function showInline(element, message, type = "error") {
  if (!element) return;
  element.hidden = false;
  element.className = `alert alert--${type}`;
  element.textContent = message;
}

export function closeDialog(dialog) {
  if (dialog?.open) dialog.close();
}

const pendingFlash = sessionStorage.getItem("rezepte-flash");
if (pendingFlash) {
  sessionStorage.removeItem("rezepte-flash");
  try {
    const flash = JSON.parse(pendingFlash);
    toast(flash.message, flash.type || "info");
  } catch {
    // Ignore malformed browser-local state.
  }
}

document.querySelector("[data-mobile-more]")?.addEventListener("click", () => {
  document.querySelector("[data-mobile-more-dialog]")?.showModal();
});
document.querySelectorAll("[data-dialog-close]").forEach((button) => {
  button.addEventListener("click", () => closeDialog(button.closest("dialog")));
});
document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) closeDialog(dialog);
  });
});
document.addEventListener("click", (event) => {
  document.querySelectorAll("details[data-dropdown][open]").forEach((dropdown) => {
    if (!(event.target instanceof Node) || !dropdown.contains(event.target)) {
      dropdown.removeAttribute("open");
    }
  });
});
document.querySelectorAll("[data-auto-submit]").forEach((input) => {
  input.addEventListener("change", () => input.form?.requestSubmit());
});

if (document.querySelector('meta[name="pwa-enabled"]')?.content === "true" && "serviceWorker" in navigator && location.protocol !== "file:") {
  window.addEventListener("load", async () => {
    try {
      const registration = await navigator.serviceWorker.register("/service-worker.js", { scope: "/" });
      registration.addEventListener("updatefound", () => {
        const worker = registration.installing;
        worker?.addEventListener("statechange", () => {
          if (worker.state === "installed" && navigator.serviceWorker.controller) {
            toast(t("pwa.update_available"), "info");
          }
        });
      });
    } catch {
      // The web app remains fully functional without service-worker support.
    }
  });
}

document.documentElement.classList.toggle(
  "is-standalone",
  matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true,
);

document.querySelectorAll('form[action="/logout"]').forEach((form) => {
  form.addEventListener("submit", () => {
    for (const storage of [window.sessionStorage, window.localStorage]) {
      for (let index = storage.length - 1; index >= 0; index -= 1) {
        const key = storage.key(index);
        if (key?.startsWith("rezepte-")) storage.removeItem(key);
      }
    }
  });
});

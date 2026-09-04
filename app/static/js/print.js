document.querySelector("[data-print]")?.addEventListener("click", () => window.print());
window.addEventListener("load", () => window.print(), { once: true });

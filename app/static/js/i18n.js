const catalogNode = document.querySelector("#app-i18n");

let catalog = {};
try {
  catalog = catalogNode ? JSON.parse(catalogNode.textContent || "{}") : {};
} catch {
  catalog = {};
}

export const locale = document.documentElement.lang || "de";

export function t(key, values = {}) {
  const template = catalog[key] || key;
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (match, name) => (
    Object.hasOwn(values, name) ? String(values[name]) : match
  ));
}

export function tp(singularKey, pluralKey, count, values = {}) {
  return t(count === 1 ? singularKey : pluralKey, { ...values, count });
}

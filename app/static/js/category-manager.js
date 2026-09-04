import { api, toast } from "./app.js";
import { locale, t } from "./i18n.js";

const manager = document.querySelector("[data-category-manager]");
const dialog = document.querySelector("[data-category-dialog]");
const detail = document.querySelector("[data-category-detail]");
const mergeDialog = document.querySelector("[data-merge-dialog]");
let selected = null;

function nodes() { return [...manager.querySelectorAll("[data-category-id]")]; }

function replaceParentOptions(select, excludeId = null) {
  const root = document.createElement("option");
  root.value = "";
  root.textContent = t("categories.root");
  const options = nodes()
    .filter((node) => node.dataset.categoryId !== excludeId)
    .map((node) => {
      const option = document.createElement("option");
      option.value = node.dataset.categoryId;
      option.textContent = node.dataset.path;
      return option;
    });
  select.replaceChildren(root, ...options);
}

function openEditor(node = null) {
  const form = dialog.querySelector("form");
  form.reset();
  form.elements.category_id.value = node?.dataset.categoryId || "";
  form.elements.name.value = node?.dataset.name || "";
  replaceParentOptions(form.elements.parent_id, node?.dataset.categoryId);
  form.elements.parent_id.value = node?.dataset.parentId || "";
  dialog.querySelector("h2").textContent = node ? t("categories.edit") : t("categories.create");
  dialog.showModal();
  form.elements.name.focus();
}

function detailRow(term, description) {
  const row = document.createElement("div");
  const dt = document.createElement("dt");
  const dd = document.createElement("dd");
  dt.textContent = term;
  dd.textContent = description;
  row.append(dt, dd);
  return row;
}

document.querySelector("[data-new-category]")?.addEventListener("click", () => openEditor());

manager?.addEventListener("click", (event) => {
  const node = event.target.closest("[data-category-id]"); if (!node) return;
  nodes().forEach((item) => item.removeAttribute("aria-current")); node.setAttribute("aria-current", "true"); selected = node;
  detail.replaceChildren();
  const card = document.createElement("div"); card.className = "category-detail-card";
  const title = document.createElement("h2"); title.textContent = node.dataset.name;
  const dl = document.createElement("dl");
  dl.append(
    detailRow(t("categories.usage"), `${node.dataset.usage} ${t(Number(node.dataset.usage) === 1 ? "recipe.one" : "recipe.other")}`),
    detailRow(t("categories.origin"), node.dataset.origin === "ai_import" ? t("categories.origin_ai") : t("categories.origin_manual")),
    detailRow(t("categories.children"), node.dataset.children),
    detailRow(t("categories.position"), String(Number(node.dataset.position) + 1)),
  );
  const actions = document.createElement("div"); actions.className = "button-group";
  const edit = document.createElement("button"); edit.className = "button button--secondary"; edit.textContent = t("common.edit"); edit.addEventListener("click", () => openEditor(node));
  const child = document.createElement("button"); child.className = "button button--secondary"; child.textContent = t("categories.child"); child.addEventListener("click", () => { openEditor(); dialog.querySelector("form").elements.parent_id.value = node.dataset.categoryId; });
  const up = document.createElement("button"); up.className = "button button--text"; up.textContent = t("categories.move_up"); up.addEventListener("click", async () => { await api(`/api/v1/categories/${node.dataset.categoryId}/move`, { method: "POST", body: JSON.stringify({ parent_id: node.dataset.parentId || null, position: Math.max(0, Number(node.dataset.position) - 1) }) }); location.reload(); });
  const down = document.createElement("button"); down.className = "button button--text"; down.textContent = t("categories.move_down"); down.addEventListener("click", async () => { await api(`/api/v1/categories/${node.dataset.categoryId}/move`, { method: "POST", body: JSON.stringify({ parent_id: node.dataset.parentId || null, position: Number(node.dataset.position) + 1 }) }); location.reload(); });
  const merge = document.createElement("button"); merge.className = "button button--secondary"; merge.textContent = t("categories.merge"); merge.addEventListener("click", () => { const form = mergeDialog.querySelector("form"); form.elements.source_id.value = node.dataset.categoryId; const options = nodes().filter((item) => item.dataset.categoryId !== node.dataset.categoryId).map((item) => { const option = document.createElement("option"); option.value = item.dataset.categoryId; option.textContent = item.dataset.path; return option; }); form.elements.target_id.replaceChildren(...options); mergeDialog.showModal(); });
  const remove = document.createElement("button"); remove.className = "button button--text danger-link"; remove.textContent = t("common.delete"); remove.addEventListener("click", async () => { if (!confirm(t("categories.delete_confirm", { path: node.dataset.path, count: node.dataset.usage }))) return; try { await api(`/api/v1/categories/${node.dataset.categoryId}`, { method: "DELETE" }); location.reload(); } catch (error) { toast(error.message, "error"); } });
  actions.append(edit, child, up, down, merge, remove); card.append(title, dl, actions); detail.append(card);
});

dialog?.querySelector("form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget; const id = form.elements.category_id.value; const payload = { name: form.elements.name.value.trim(), parent_id: form.elements.parent_id.value || null };
  try { const result = await api(id ? `/api/v1/categories/${id}` : "/api/v1/categories", { method: id ? "PUT" : "POST", body: JSON.stringify(payload) }); toast(result.message); location.reload(); } catch (error) { toast(error.message, "error"); }
});
mergeDialog?.querySelector("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    const result = await api(`/api/v1/categories/${form.elements.source_id.value}/merge`, { method: "POST", body: JSON.stringify({ target_category_id: form.elements.target_id.value }) });
    toast(result.message); location.reload();
  } catch (error) { toast(error.message, "error"); }
});
manager?.querySelector("[data-category-filter]").addEventListener("input", (event) => { const query = event.target.value.trim().toLocaleLowerCase(locale); nodes().forEach((node) => { node.hidden = query && !node.dataset.path.toLocaleLowerCase(locale).includes(query); }); });

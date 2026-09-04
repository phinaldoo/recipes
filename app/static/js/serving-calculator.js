import { api, toast } from "./app.js";
import { locale, t } from "./i18n.js";

const root = document.querySelector("[data-recipe-id]");
if (root) {
  const base = Number(root.dataset.baseServings);
  const input = root.querySelector("[data-servings-input]");
  const note = root.querySelector("[data-servings-note]");
  const format = new Intl.NumberFormat(locale, { maximumFractionDigits: 3 });
  const fractionMap = new Map([[0.25, "¼"], [0.5, "½"], [0.75, "¾"]]);

  input.value = String(base);

  function amount(value) {
    const rounded = Math.round(value * 1000) / 1000;
    const whole = Math.floor(rounded);
    const fraction = Math.round((rounded - whole) * 100) / 100;
    if (fractionMap.has(fraction)) return `${whole || ""}${fractionMap.get(fraction)}`;
    return format.format(rounded);
  }

  function update() {
    const desired = Number(input.value);
    if (!Number.isFinite(desired) || desired <= 0) return;
    root.querySelectorAll("[data-ingredient]").forEach((row) => {
      const scalable = row.dataset.scalable === "true";
      const min = Number(row.dataset.amountMin);
      const max = Number(row.dataset.amountMax);
      if (row.dataset.amountMin && scalable) row.querySelector("[data-scaled-min]").textContent = amount(min * desired / base);
      if (row.dataset.amountMax && scalable) row.querySelector("[data-scaled-max]").textContent = amount(max * desired / base);
    });
    note.textContent = t("servings.current", {
      desired: format.format(desired),
      label: root.dataset.servingLabel,
      base: format.format(base),
    });
    const query = `servings=${encodeURIComponent(desired)}`;
    root.querySelector("[data-pdf-export]").href = `/api/v1/recipes/${root.dataset.recipeId}/export/pdf?${query}`;
    root.querySelector("[data-pdf-comments]").href = `/api/v1/recipes/${root.dataset.recipeId}/export/pdf?${query}&include_comments=true`;
    root.querySelector("[data-print-link]").href = `/rezepte/${root.dataset.recipeId}/print?${query}`;
    root.querySelector("[data-print-comments]").href = `/rezepte/${root.dataset.recipeId}/print?${query}&include_comments=true`;
  }
  input.addEventListener("input", update);
  root.querySelector("[data-servings-minus]").addEventListener("click", () => { input.value = Math.max(0.25, Number(input.value || base) - 0.25); update(); });
  root.querySelector("[data-servings-plus]").addEventListener("click", () => { input.value = Number(input.value || base) + 0.25; update(); });
  update();

  root.querySelectorAll("[data-gallery-image]").forEach((button) => {
    button.addEventListener("click", () => {
      const figure = root.querySelector(".recipe-gallery__main");
      const main = figure.querySelector("img");
      const caption = figure.querySelector("figcaption");
      const generated = figure.querySelector("[data-gallery-generated]");
      const thumbnail = button.querySelector("img");
      const old = {
        src: main.src,
        alt: main.alt,
        caption: caption.textContent,
        generated: generated ? !generated.hidden : false,
      };
      main.src = button.dataset.src;
      main.alt = button.dataset.alt;
      caption.textContent = button.dataset.caption;
      caption.hidden = !button.dataset.caption;
      if (generated) generated.hidden = button.dataset.generated !== "true";
      button.dataset.src = old.src;
      button.dataset.alt = old.alt;
      button.dataset.caption = old.caption;
      button.dataset.generated = String(old.generated);
      button.setAttribute("aria-label", t("recipe.image.show", { name: old.alt }));
      thumbnail.src = old.src;
    });
  });

  const deleteDialog = document.querySelector("[data-delete-dialog]");
  root.querySelector("[data-delete-recipe]")?.addEventListener("click", () => deleteDialog.showModal());
  deleteDialog?.querySelector("[data-delete-form]").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (event.submitter?.value !== "confirm") { deleteDialog.close(); return; }
    try {
      const result = await api(`/api/v1/recipes/${root.dataset.recipeId}`, { method: "DELETE" });
      toast(result.message); location.assign(result.redirect);
    } catch (error) { toast(error.message, "error"); }
  });
}

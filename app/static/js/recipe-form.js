import { ApiError, api, toast } from "./app.js";
import { locale, t, tp } from "./i18n.js";
import { RecipeImageQueue, transferredFiles } from "./lib/recipe-image-queue.js";

const form = document.querySelector("[data-recipe-form]");
if (!form) throw new Error("Rezeptformular fehlt");

let dirty = false;
let saving = false;
const ingredientTemplate = document.querySelector("#ingredient-row-template");
const groupTemplate = document.querySelector("#ingredient-group-template");
const stepTemplate = document.querySelector("#step-row-template");
const imageFiles = document.querySelector("[data-image-files]");
const imagePasteZone = document.querySelector("[data-image-paste-zone]");
const imagePreview = document.querySelector("[data-image-preview]");
const imageMessage = document.querySelector("[data-image-message]");
const pendingImageQueue = new RecipeImageQueue();
const imagePreviewUrls = new Set();

function decimal(value) {
  const cleaned = value.trim().replace(/\s/g, "").replace(",", ".");
  return cleaned === "" ? null : cleaned;
}

function integer(value) {
  return value === "" ? null : Number.parseInt(value, 10);
}

function markDirty() { dirty = true; }
form.addEventListener("input", markDirty);
form.addEventListener("change", markDirty);
window.addEventListener("beforeunload", (event) => {
  if (dirty && !saving) event.preventDefault();
});

function renumber() {
  form.querySelectorAll("[data-ingredient-group]").forEach((group, index) => {
    const legend = group.querySelector("legend");
    if (legend) legend.textContent = t("form.ingredient_group", { number: index + 1 });
  });
  form.querySelectorAll("[data-step-row]").forEach((row, index) => {
    row.querySelector(".step-row__number").textContent = String(index + 1);
    row.querySelector("[data-step-text]").setAttribute("aria-label", t("form.step", { number: index + 1 }));
  });
}

function moveRow(button, direction) {
  const row = button.closest("[data-ingredient-row], [data-step-row]");
  if (!row) return;
  const sibling = direction < 0 ? row.previousElementSibling : row.nextElementSibling;
  if (!sibling) return;
  if (direction < 0) sibling.before(row); else sibling.after(row);
  markDirty();
  renumber();
  button.focus();
}

form.addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.matches("[data-add-ingredient]")) {
    const row = ingredientTemplate.content.firstElementChild.cloneNode(true);
    button.closest("[data-ingredient-group]").querySelector("[data-ingredient-rows]").append(row);
    row.querySelector("[data-name]").focus();
    markDirty();
  } else if (button.matches("[data-remove-row]")) {
    button.closest("[data-ingredient-row]").remove(); markDirty();
  } else if (button.matches("[data-add-group]")) {
    const group = groupTemplate.content.firstElementChild.cloneNode(true);
    form.querySelector("[data-ingredient-groups]").append(group);
    group.querySelector("[data-add-ingredient]").click();
    renumber(); markDirty();
  } else if (button.matches("[data-remove-group]")) {
    if (form.querySelectorAll("[data-ingredient-group]").length === 1) {
      toast(t("form.empty_group_kept"), "info"); return;
    }
    button.closest("[data-ingredient-group]").remove(); renumber(); markDirty();
  } else if (button.matches("[data-add-step]")) {
    const row = stepTemplate.content.firstElementChild.cloneNode(true);
    form.querySelector("[data-step-list]").append(row);
    renumber(); row.querySelector("textarea").focus(); markDirty();
  } else if (button.matches("[data-remove-step]")) {
    button.closest("[data-step-row]").remove(); renumber(); markDirty();
  } else if (button.matches("[data-move-up]")) moveRow(button, -1);
  else if (button.matches("[data-move-down]")) moveRow(button, 1);
});

if (!form.querySelector("[data-ingredient-row]")) form.querySelector("[data-add-ingredient]")?.click();
if (!form.querySelector("[data-step-row]")) form.querySelector("[data-add-step]")?.click();
dirty = false;

function updateTotal() {
  const manual = form.elements.total_time_is_manual.checked;
  const total = form.elements.total_time_minutes;
  total.readOnly = !manual;
  if (!manual) {
    total.value = ["prep_time_minutes", "cook_time_minutes", "rest_time_minutes"]
      .map((name) => Number.parseInt(form.elements[name].value || "0", 10))
      .reduce((sum, value) => sum + value, 0) || "";
  }
}
["prep_time_minutes", "cook_time_minutes", "rest_time_minutes", "total_time_is_manual"].forEach((name) => {
  form.elements[name].addEventListener("input", updateTotal);
  form.elements[name].addEventListener("change", updateTotal);
});
updateTotal();

const categoryPicker = form.querySelector("[data-category-picker]");
const categoryCount = form.querySelector("[data-category-count]");
const selectedContainer = form.querySelector("[data-selected-categories]");

function selectedCategories() {
  return [...categoryPicker.querySelectorAll("[data-category-option] input:checked")].map((input) => ({
    id: input.value.startsWith("new:") ? null : input.value,
    path: input.dataset.path.split("›").map((part) => part.trim()).filter(Boolean),
    origin: input.dataset.origin || "manual",
    input,
  }));
}

function renderSelectedCategories() {
  const selected = selectedCategories();
  categoryCount.textContent = t("form.category_count", { count: selected.length });
  selectedContainer.replaceChildren();
  selected.forEach((item) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip chip--active";
    chip.textContent = `${item.path.join(" › ")} ×`;
    chip.setAttribute("aria-label", t("form.remove_category", { name: item.path.join(" › ") }));
    chip.addEventListener("click", () => { item.input.checked = false; item.input.closest("[data-category-option]")?.removeAttribute("data-new"); renderSelectedCategories(); markDirty(); });
    selectedContainer.append(chip);
  });
  categoryPicker.querySelectorAll("[data-category-option] input:not(:checked)").forEach((input) => {
    input.disabled = selected.length >= 20;
  });
}
categoryPicker.addEventListener("change", (event) => {
  if (event.target.matches("[data-category-option] input")) renderSelectedCategories();
});
categoryPicker.querySelector("[data-category-search]").addEventListener("input", (event) => {
  const query = event.target.value.trim().toLocaleLowerCase(locale);
  categoryPicker.querySelectorAll("[data-category-option]").forEach((option) => {
    option.hidden = query && !option.dataset.search.includes(query);
  });
});
categoryPicker.querySelector("[data-add-category-path]").addEventListener("click", () => {
  if (selectedCategories().length >= 20) { toast(t("form.max_categories"), "error"); return; }
  const input = categoryPicker.querySelector("[data-new-category]");
  const parts = input.value.split("›").map((part) => part.trim()).filter(Boolean);
  if (!parts.length) { input.focus(); return; }
  const path = parts.join(" › ");
  const duplicate = [...categoryPicker.querySelectorAll("[data-category-option] input")]
    .find((item) => item.dataset.path.toLocaleLowerCase(locale) === path.toLocaleLowerCase(locale));
  if (duplicate) { duplicate.checked = true; input.value = ""; renderSelectedCategories(); return; }
  const label = document.createElement("label");
  label.className = "check-row";
  label.dataset.categoryOption = "";
  label.dataset.new = "true";
  label.dataset.search = path.toLocaleLowerCase(locale);
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox"; checkbox.checked = true; checkbox.value = `new:${crypto.randomUUID()}`; checkbox.dataset.path = path;
  const text = document.createElement("span"); text.textContent = `${path} · ${t("form.new")}`;
  label.append(checkbox, text);
  categoryPicker.querySelector(".category-options").append(label);
  input.value = ""; renderSelectedCategories(); markDirty();
});
renderSelectedCategories();

function revokeImagePreviewUrls() {
  imagePreviewUrls.forEach((url) => URL.revokeObjectURL(url));
  imagePreviewUrls.clear();
}

function formatFileSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.ceil(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toLocaleString(locale, { maximumFractionDigits: 1 })} MB`;
}

function showImageMessage(message, type = "info") {
  if (!imageMessage) return;
  imageMessage.hidden = false;
  imageMessage.className = type === "info" ? "image-upload-message" : `image-upload-message alert alert--${type}`;
  imageMessage.textContent = message;
}

function renderPendingImages() {
  if (!imagePreview) return;
  revokeImagePreviewUrls();
  imagePreview.replaceChildren();
  const entries = pendingImageQueue.entries;
  imagePreview.hidden = entries.length === 0;

  entries.forEach((entry, index) => {
    const figure = document.createElement("figure");
    const image = document.createElement("img");
    image.alt = "";
    const previewUrl = URL.createObjectURL(entry.file);
    imagePreviewUrls.add(previewUrl);
    image.src = previewUrl;
    const releasePreview = () => {
      if (!imagePreviewUrls.delete(previewUrl)) return;
      URL.revokeObjectURL(previewUrl);
    };
    image.addEventListener("load", releasePreview, { once: true });
    image.addEventListener("error", releasePreview, { once: true });

    const caption = document.createElement("figcaption");
    caption.className = "upload-preview__caption";
    const title = document.createElement("strong");
    title.textContent = index === 0 ? t("form.new_cover") : t("form.new_image", { number: index + 1 });
    const details = document.createElement("span");
    const source = entry.source === "clipboard"
      ? t("form.image_source.clipboard")
      : entry.source === "drop"
        ? t("form.image_source.drop")
        : t("form.image_source.selected");
    details.textContent = `${source} · ${entry.name} · ${formatFileSize(entry.file.size)}`;
    caption.append(title, details);

    const remove = document.createElement("button");
    remove.className = "icon-button danger-link upload-preview__remove";
    remove.type = "button";
    remove.setAttribute("aria-label", `${t("common.remove")}: ${entry.name}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      pendingImageQueue.remove(index);
      renderPendingImages();
      const remaining = pendingImageQueue.size;
      showImageMessage(remaining === 0
        ? t("form.no_new_images")
        : t("form.new_images_queued", { count: remaining }));
      markDirty();
    });

    figure.append(image, caption, remove);
    imagePreview.append(figure);
  });
}

function addPendingImages(fileList, source) {
  const { added, rejected } = pendingImageQueue.add(fileList, source);
  if (added.length > 0) {
    renderPendingImages();
    markDirty();
  }

  if (added.length > 0 && rejected.length === 0) {
    const count = added.length;
    showImageMessage(tp("form.images_added.one", "form.images_added.other", count));
  } else if (added.length > 0) {
    showImageMessage(t("form.images_partial", { added: added.length, rejected: rejected.length }), "warning");
  } else if (rejected.length > 0) {
    showImageMessage(t("form.image_format_error"), "warning");
  }
}

imageFiles?.addEventListener("change", () => {
  const selectedFiles = [...imageFiles.files];
  // Keep a separate queue so repeated selections and clipboard images stay together.
  imageFiles.value = "";
  addPendingImages(selectedFiles, "picker");
});

const imageSection = imagePasteZone?.closest("#bilder");
imageSection?.addEventListener("paste", (event) => {
  const files = transferredFiles(event.clipboardData);
  if (files.length === 0) {
    if (imagePasteZone.contains(event.target)) {
      showImageMessage(t("form.clipboard_no_image"), "warning");
    }
    return;
  }
  event.preventDefault();
  addPendingImages(files, "clipboard");
});

let imageDragDepth = 0;
function containsDraggedFiles(dataTransfer) {
  return Array.from(dataTransfer?.types || []).includes("Files")
    || Array.from(dataTransfer?.items || []).some((item) => item.kind === "file");
}

imagePasteZone?.addEventListener("dragenter", (event) => {
  if (!containsDraggedFiles(event.dataTransfer)) return;
  event.preventDefault();
  imageDragDepth += 1;
  imagePasteZone.classList.add("is-dragging");
});
imagePasteZone?.addEventListener("dragover", (event) => {
  if (!containsDraggedFiles(event.dataTransfer)) return;
  event.preventDefault();
  event.dataTransfer.dropEffect = "copy";
});
imagePasteZone?.addEventListener("dragleave", () => {
  imageDragDepth = Math.max(0, imageDragDepth - 1);
  if (imageDragDepth === 0) imagePasteZone.classList.remove("is-dragging");
});
imagePasteZone?.addEventListener("drop", (event) => {
  event.preventDefault();
  imageDragDepth = 0;
  imagePasteZone.classList.remove("is-dragging");
  const files = transferredFiles(event.dataTransfer);
  if (files.length === 0) {
    showImageMessage(t("form.drop_image"), "warning");
    return;
  }
  addPendingImages(files, "drop");
});
imagePasteZone?.addEventListener("keydown", (event) => {
  if (event.target !== imagePasteZone || !["Enter", " "].includes(event.key)) return;
  event.preventDefault();
  imageFiles?.click();
});
window.addEventListener("pagehide", revokeImagePreviewUrls);

form.querySelectorAll("[data-existing-image]").forEach((figure) => {
  figure.querySelector("[data-set-cover]")?.addEventListener("click", async (event) => {
    await api(`/api/v1/recipes/${form.dataset.recipeId}/images/${figure.dataset.imageId}`, { method: "PUT", body: JSON.stringify({ is_cover: true }) });
    location.reload();
  });
  figure.querySelector("[data-remove-image]")?.addEventListener("click", async () => {
    if (!confirm(t("form.remove_image_confirm"))) return;
    await api(`/api/v1/recipes/${form.dataset.recipeId}/images/${figure.dataset.imageId}`, { method: "DELETE" });
    figure.remove(); toast(t("form.image_removed"));
  });
  figure.querySelector("[data-save-image]")?.addEventListener("click", async () => {
    await api(`/api/v1/recipes/${form.dataset.recipeId}/images/${figure.dataset.imageId}`, {
      method: "PUT",
      body: JSON.stringify({
        caption: figure.querySelector("[data-image-caption]").value,
        alt_text: figure.querySelector("[data-image-alt]").value,
      }),
    });
    toast(t("form.image_saved"));
  });
  const moveImage = async (direction) => {
    const figures = [...form.querySelectorAll("[data-existing-image]")];
    const current = figures.indexOf(figure);
    const target = current + direction;
    if (target < 0 || target >= figures.length) return;
    await api(`/api/v1/recipes/${form.dataset.recipeId}/images/${figure.dataset.imageId}`, {
      method: "PUT",
      body: JSON.stringify({ position: target }),
    });
    if (direction < 0) figures[target].before(figure); else figures[target].after(figure);
    toast(t("form.image_order_saved"));
  };
  figure.querySelector("[data-move-image-up]")?.addEventListener("click", () => moveImage(-1));
  figure.querySelector("[data-move-image-down]")?.addEventListener("click", () => moveImage(1));
});

function collectPayload() {
  const groups = [...form.querySelectorAll("[data-ingredient-group]")].map((group) => ({
    title: group.querySelector("[data-group-title]").value.trim() || null,
    ingredients: [...group.querySelectorAll("[data-ingredient-row]")]
      .map((row) => ({
        amount_min: decimal(row.querySelector("[data-amount-min]").value),
        amount_max: decimal(row.querySelector("[data-amount-max]").value),
        unit: row.querySelector("[data-unit]").value.trim() || null,
        name: row.querySelector("[data-name]").value.trim(),
        note: row.querySelector("[data-note]").value.trim() || null,
        is_scalable: row.querySelector("[data-scalable]").checked,
      }))
      .filter((item) => item.name || item.amount_min || item.unit || item.note),
  }));
  const nutrition = [...form.querySelectorAll("[data-nutrition-row]")]
    .map((row) => {
      const values = Object.fromEntries(
        [...row.querySelectorAll("[data-nutrition-field]")].map((input) => [
          input.dataset.nutritionField,
          decimal(input.value),
        ]),
      );
      return {
        basis: row.dataset.basis,
        ...values,
        note: row.querySelector("[data-nutrition-note]").value.trim() || null,
      };
    })
    .filter((value) => Object.entries(value).some(([field, item]) => field !== "basis" && item !== null));
  return {
    title: form.elements.title.value.trim(),
    description: form.elements.description.value.trim() || null,
    recipe_kind: form.elements.recipe_kind.value,
    base_servings: decimal(form.elements.base_servings.value),
    serving_label: form.elements.serving_label.value.trim(),
    prep_time_minutes: integer(form.elements.prep_time_minutes.value),
    cook_time_minutes: integer(form.elements.cook_time_minutes.value),
    rest_time_minutes: integer(form.elements.rest_time_minutes.value),
    total_time_minutes: integer(form.elements.total_time_minutes.value),
    total_time_is_manual: form.elements.total_time_is_manual.checked,
    nutrition,
    notes: form.elements.notes.value.trim() || null,
    status: "active",
    ingredient_groups: groups,
    instruction_steps: [...form.querySelectorAll("[data-step-text]")].map((step) => ({ text: step.value.trim() })).filter((step) => step.text),
    categories: selectedCategories().map(({ id, path, origin }) => ({ id, path, origin })),
    tags: form.elements.tags.value.split(",").map((tag) => tag.trim()).filter(Boolean),
    source: (form.elements.source_title.value.trim() || form.elements.source_url.value.trim()) ? { title: form.elements.source_title.value.trim() || null, url: form.elements.source_url.value.trim() || null } : null,
    expected_updated_at: form.elements.expected_updated_at?.value || null,
  };
}

function showErrors(error) {
  const summary = form.querySelector("[data-form-errors]");
  const list = summary.querySelector("ul");
  list.replaceChildren();
  form.querySelectorAll("[data-error-for]").forEach((item) => { item.textContent = ""; });
  const fields = error.payload?.error?.fields || [];
  if (fields.length) {
    fields.forEach((item) => {
      const li = document.createElement("li"); li.textContent = item.message; list.append(li);
      const field = item.field?.split(".")[0];
      const target = form.querySelector(`[data-error-for="${CSS.escape(field)}"]`);
      if (target) target.textContent = item.message;
    });
  } else { const li = document.createElement("li"); li.textContent = error.message; list.append(li); }
  summary.hidden = false; summary.focus();
}

async function uploadImages(recipeId, onProgress) {
  const entries = pendingImageQueue.entries;
  for (const [index, entry] of entries.entries()) {
    onProgress?.(index + 1, entries.length);
    const body = new FormData();
    body.append("file", entry.file, entry.name);
    body.append("is_cover", index === 0 ? "true" : "false");
    await api(`/api/v1/recipes/${recipeId}/images`, { method: "POST", body });
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;
  const payload = collectPayload();
  if (!payload.title || !payload.base_servings || Number(payload.base_servings) <= 0) {
    showErrors(new ApiError(t("form.required_error"), 422, null)); return;
  }
  const button = form.querySelector("[data-save-button]");
  button.disabled = true; button.textContent = t("form.saving"); saving = true;
  try {
    const editing = form.dataset.mode === "edit";
    const result = await api(editing ? `/api/v1/recipes/${form.dataset.recipeId}` : "/api/v1/recipes", { method: editing ? "PUT" : "POST", body: JSON.stringify(payload) });
    const recipeId = editing ? form.dataset.recipeId : result.recipe.id;
    try {
      await uploadImages(recipeId, (current, total) => {
        button.textContent = t("form.uploading_image", { current, total });
      });
    } catch (error) {
      dirty = false;
      const detail = error instanceof Error ? ` ${error.message}` : "";
      sessionStorage.setItem("rezepte-flash", JSON.stringify({
        type: "error",
        message: `${t("form.saved_images_error")}${detail}`,
      }));
      location.assign(`/rezepte/${recipeId}/bearbeiten`);
      return;
    }
    dirty = false;
    location.assign(`/rezepte/${recipeId}`);
  } catch (error) {
    saving = false; button.disabled = false; button.textContent = form.dataset.mode === "edit" ? t("form.save_changes") : t("form.save_recipe");
    showErrors(error instanceof ApiError ? error : new ApiError(t("form.save_error"), 0, null));
  }
});

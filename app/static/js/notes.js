import { ApiError, api, showInline } from "./app.js";
import { t } from "./i18n.js";

const form = document.querySelector("[data-note-form]");
const formTitle = document.querySelector("[data-note-form-title]");
const submitButton = document.querySelector("[data-note-submit]");
const cancelButton = document.querySelector("[data-note-cancel]");
const errorMessage = document.querySelector("[data-note-error]");
let editingId = null;

function field(name) {
  return form?.elements.namedItem(name);
}

function clearError() {
  if (!errorMessage) return;
  errorMessage.hidden = true;
  errorMessage.textContent = "";
}

function resetEditor() {
  editingId = null;
  form?.reset();
  clearError();
  if (formTitle) formTitle.textContent = t("notes.new");
  if (submitButton) submitButton.textContent = t("notes.save");
  if (cancelButton) cancelButton.hidden = true;
}

function payload() {
  return {
    title: field("title")?.value || null,
    url: field("url")?.value || null,
    content: field("content")?.value || null,
  };
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  if (!form.reportValidity()) return;
  const values = payload();
  if (!Object.values(values).some((value) => value?.trim())) {
    showInline(errorMessage, t("notes.required"));
    field("title")?.focus();
    return;
  }

  submitButton.disabled = true;
  submitButton.setAttribute("aria-busy", "true");
  try {
    const result = await api(editingId ? `/api/v1/notes/${editingId}` : "/api/v1/notes", {
      method: editingId ? "PUT" : "POST",
      body: JSON.stringify(values),
    });
    sessionStorage.setItem("rezepte-flash", JSON.stringify({ message: result.message }));
    location.reload();
  } catch (error) {
    showInline(
      errorMessage,
      error instanceof ApiError ? error.message : t("notes.save_error"),
    );
  } finally {
    submitButton.disabled = false;
    submitButton.removeAttribute("aria-busy");
  }
});

cancelButton?.addEventListener("click", resetEditor);

document.querySelectorAll("[data-note-id]").forEach((card) => {
  card.querySelector("[data-note-edit]")?.addEventListener("click", () => {
    let values;
    try {
      values = JSON.parse(card.dataset.notePayload || "{}");
    } catch {
      showInline(errorMessage, t("notes.open_error"));
      return;
    }
    editingId = card.dataset.noteId;
    field("title").value = values.title || "";
    field("url").value = values.url || "";
    field("content").value = values.content || "";
    clearError();
    if (formTitle) formTitle.textContent = t("notes.edit");
    if (submitButton) submitButton.textContent = t("form.save_changes");
    if (cancelButton) cancelButton.hidden = false;
    form.scrollIntoView({ behavior: "smooth", block: "start" });
    field("title")?.focus({ preventScroll: true });
  });

  card.querySelector("[data-note-delete]")?.addEventListener("click", async (event) => {
    if (!confirm(t("notes.delete_confirm"))) return;
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const result = await api(`/api/v1/notes/${card.dataset.noteId}`, { method: "DELETE" });
      sessionStorage.setItem("rezepte-flash", JSON.stringify({ message: result.message }));
      location.reload();
    } catch (error) {
      showInline(
        errorMessage,
        error instanceof ApiError ? error.message : t("notes.delete_error"),
      );
      button.disabled = false;
    }
  });
});

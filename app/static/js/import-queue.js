import { api, showInline, toast } from "./app.js";
import { locale, t, tp } from "./i18n.js";
import { ImportFileQueue } from "./lib/import-file-queue.js";

const message = document.querySelector("[data-import-message]");
const fileImportForm = document.querySelector("[data-file-import]");
const fileInput = document.querySelector("#import-files");
const fileSelection = document.querySelector("[data-file-selection]");
const fileSelectionMessage = document.querySelector("[data-file-selection-message]");
const importFileQueue = new ImportFileQueue(20);
const previewUrls = new Set();

function clearSelectionMessage() {
  if (!fileSelectionMessage) return;
  fileSelectionMessage.hidden = true;
  fileSelectionMessage.textContent = "";
}

function revokePreviewUrls() {
  previewUrls.forEach((url) => URL.revokeObjectURL(url));
  previewUrls.clear();
}

function formatFileSize(bytes) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.ceil(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toLocaleString(locale, { maximumFractionDigits: 1 })} MB`;
}

function isImageFile(file) {
  return file.type.startsWith("image/") || /\.(?:avif|gif|heic|heif|jpe?g|png|webp)$/i.test(file.name);
}

function renderSelectedFiles() {
  if (!fileSelection || !fileInput || !fileImportForm) return;
  revokePreviewUrls();
  fileSelection.replaceChildren();

  const files = importFileQueue.files;
  fileSelection.hidden = files.length === 0;
  fileInput.disabled = files.length >= importFileQueue.limit;
  const submit = fileImportForm.querySelector('button[type="submit"]');
  if (submit) submit.disabled = files.length === 0;
  if (files.length === 0) return;

  const summary = document.createElement("p");
  summary.className = "file-selection__summary";
  summary.textContent = t("import.selected_files", { count: files.length, limit: importFileQueue.limit });
  fileSelection.append(summary);

  files.forEach((file, index) => {
    const row = document.createElement("div");
    row.className = "file-selection__item";

    if (isImageFile(file)) {
      const preview = document.createElement("img");
      const previewUrl = URL.createObjectURL(file);
      previewUrls.add(previewUrl);
      preview.alt = "";
      preview.src = previewUrl;
      const releasePreview = () => {
        if (!previewUrls.delete(previewUrl)) return;
        URL.revokeObjectURL(previewUrl);
      };
      preview.addEventListener("load", releasePreview, { once: true });
      preview.addEventListener("error", releasePreview, { once: true });
      row.append(preview);
    } else {
      const fileIcon = document.createElement("span");
      fileIcon.className = "file-selection__file-icon";
      fileIcon.setAttribute("aria-hidden", "true");
      fileIcon.textContent = "PDF";
      row.append(fileIcon);
    }

    const copy = document.createElement("span");
    copy.className = "file-selection__copy";
    const name = document.createElement("strong");
    name.className = "file-selection__name";
    name.textContent = file.name;
    const size = document.createElement("span");
    size.className = "file-selection__size";
    size.textContent = formatFileSize(file.size);
    copy.append(name, size);
    row.append(copy);

    const remove = document.createElement("button");
    remove.className = "icon-button danger-link";
    remove.type = "button";
    remove.setAttribute("aria-label", `${t("common.remove")}: ${file.name}`);
    remove.textContent = "×";
    remove.addEventListener("click", () => {
      importFileQueue.remove(index);
      clearSelectionMessage();
      renderSelectedFiles();
    });
    row.append(remove);
    fileSelection.append(row);
  });
}

fileInput?.addEventListener("change", () => {
  const { rejected } = importFileQueue.add(fileInput.files);

  // A camera capture replaces the native FileList. Keep our own queue and clear the
  // input so another capture (even with the same filename) fires a fresh change event.
  fileInput.value = "";
  clearSelectionMessage();
  renderSelectedFiles();

  if (rejected.length > 0) {
    showInline(
      fileSelectionMessage,
      t("import.file_limit", { limit: importFileQueue.limit, count: rejected.length }),
      "warning",
    );
  }
});

window.addEventListener("pagehide", revokePreviewUrls);
renderSelectedFiles();

function uploadWithProgress(url, body, progress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest(); request.open("POST", url); request.responseType = "json";
    request.setRequestHeader("X-CSRF-Token", document.querySelector('meta[name="csrf-token"]')?.content || "");
    request.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return; progress.hidden = false; progress.value = Math.round((event.loaded / event.total) * 100);
    });
    request.addEventListener("load", () => {
      const payload = request.response || {};
      if (request.status >= 200 && request.status < 300) resolve(payload);
      else reject(new Error(payload.error?.message || payload.detail || t("import.upload_failed")));
    });
    request.addEventListener("error", () => reject(new Error(t("import.server_unreachable"))));
    request.send(body);
  });
}

fileImportForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const files = importFileQueue.files;
  if (files.length === 0) {
    showInline(fileSelectionMessage, t("import.select_file"));
    return;
  }
  const body = new FormData();
  files.forEach((file) => body.append("files", file, file.name));
  const progress = form.querySelector("[data-upload-progress]");
  try { const result = await uploadWithProgress("/api/v1/imports/files", body, progress); location.assign(result.redirect); } catch (error) { showInline(message, error.message); }
});
document.querySelector("[data-url-import]")?.addEventListener("submit", async (event) => {
  event.preventDefault(); const urls = event.currentTarget.querySelector("textarea").value.split(/\n+/).map((item) => item.trim()).filter(Boolean);
  try { const result = await api("/api/v1/imports/urls", { method: "POST", body: JSON.stringify({ urls }) }); location.assign(result.redirect); } catch (error) { showInline(message, error.message); }
});
document.querySelector("[data-json-import]")?.addEventListener("submit", async (event) => {
  event.preventDefault(); const input = event.currentTarget.querySelector("input[type='file']"); const body = new FormData(); body.append("file", input.files[0]);
  try { const result = await api("/api/v1/imports/json", { method: "POST", body }); toast(result.message); location.assign(result.redirect); } catch (error) { showInline(message, error.message); }
});

const batchElements = [...document.querySelectorAll("[data-batch-id]")];
let importPollTimer;

function batchStatusLabel(status) {
  return {
    queued: t("import.status.queued"),
    processing: t("import.status.processing"),
    review: t("import.status.review"),
    completed: t("import.status.completed"),
    completed_with_errors: t("import.status.completed_errors"),
  }[status] || status;
}

function actionLink(className, href, text) {
  const link = document.createElement("a");
  link.className = className;
  link.href = href;
  link.textContent = text;
  return link;
}

function renderJobActions(actions, job) {
  actions.replaceChildren();
  if (job.source_asset_id) {
    const source = actionLink("button button--text", `/api/v1/imports/jobs/${job.id}/source`, t("import.open_original"));
    source.target = "_blank";
    source.rel = "noopener";
    actions.append(source);
  }
  if (job.status === "failed") {
    const retry = document.createElement("button");
    retry.className = "button button--secondary";
    retry.type = "button";
    retry.dataset.jobRetry = "";
    retry.textContent = t("common.retry");
    actions.append(retry);
  } else if (job.status === "queued") {
    const cancel = document.createElement("button");
    cancel.className = "button button--text";
    cancel.type = "button";
    cancel.dataset.jobCancel = "";
    cancel.textContent = t("common.cancel");
    actions.append(cancel);
  } else if (job.result_recipe_id) {
    actions.append(actionLink("button button--primary", `/rezepte/${job.result_recipe_id}`, t("import.open_recipe")));
  }
}

function updateBatch(batchElement, data) {
  const tracksPageState = batchElement.hasAttribute("data-batch-status-value");
  const reachedFinalPageState = ["review", "completed", "completed_with_errors"].includes(data.status);
  if (tracksPageState
    && reachedFinalPageState
    && batchElement.dataset.batchStatusValue !== data.status) {
    location.reload();
    return;
  }
  if (tracksPageState) batchElement.dataset.batchStatusValue = data.status;
  const completed = batchElement.querySelector("[data-batch-completed]");
  const failed = batchElement.querySelector("[data-batch-failed]");
  const status = batchElement.querySelector("[data-batch-status]");
  if (completed) completed.textContent = data.completed_jobs;
  if (failed) failed.textContent = data.failed_jobs;
  if (status) status.textContent = batchStatusLabel(data.status);
  if (["review", "completed", "completed_with_errors"].includes(data.status)) {
    batchElement.removeAttribute("data-batch-poll");
  } else {
    batchElement.setAttribute("data-batch-poll", "");
  }

  data.jobs.forEach((job) => {
    const card = batchElement.querySelector(`[data-job-id="${CSS.escape(job.id)}"]`);
    if (!card) return;
    card.querySelector("[data-job-status]").textContent = job.current_stage;
    card.querySelector("[data-job-progress]").value = job.progress;
    const error = card.querySelector("[data-job-error]");
    error.hidden = !job.error_message;
    error.textContent = job.error_message || "";
    renderJobActions(card.querySelector("[data-job-actions]"), job);
  });
}

async function refreshImports() {
  window.clearTimeout(importPollTimer);
  const pollingBatches = batchElements.filter((batchElement) => batchElement.hasAttribute("data-batch-poll"));
  if (pollingBatches.length === 0) return;
  const results = await Promise.allSettled(pollingBatches.map(async (batchElement) => {
    const data = await api(`/api/v1/imports/batches/${batchElement.dataset.batchId}`);
    updateBatch(batchElement, data);
    return data;
  }));
  const hasActiveImports = results.some((result) => result.status === "fulfilled"
    && !["review", "completed", "completed_with_errors"].includes(result.value.status));
  const hasRequestError = results.some((result) => result.status === "rejected");
  if (hasActiveImports || hasRequestError) {
    importPollTimer = window.setTimeout(refreshImports, hasRequestError ? 5000 : 2500);
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-job-retry], [data-job-cancel]");
  if (!button) return;
  const card = button.closest("[data-job-id]");
  if (!card) return;
  const retry = button.hasAttribute("data-job-retry");
  const error = card.querySelector("[data-job-error]");
  button.disabled = true;
  button.textContent = retry ? t("import.restarting") : t("import.cancelling");
  try {
    const result = await api(`/api/v1/imports/jobs/${card.dataset.jobId}/${retry ? "retry" : "cancel"}`, { method: "POST" });
    toast(result.message);
    card.closest("[data-batch-id]")?.setAttribute("data-batch-poll", "");
    await refreshImports();
  } catch (requestError) {
    error.hidden = false;
    error.textContent = requestError.message;
    button.disabled = false;
    button.textContent = retry ? t("common.retry") : t("common.cancel");
  }
});

if (batchElements.length > 0) refreshImports();

const candidateReview = document.querySelector("[data-candidate-review]");
if (candidateReview) {
  const candidateInputs = [...candidateReview.querySelectorAll("[data-candidate-select]:not(:disabled)")];
  const selectedCount = candidateReview.querySelector("[data-candidate-selected-count]");
  const toggleAll = candidateReview.querySelector("[data-candidate-toggle-all]");
  const confirm = candidateReview.querySelector("[data-confirm-candidates]");
  const candidateMessage = candidateReview.querySelector("[data-candidate-message]");

  const currentSelectedCandidateIds = () => candidateInputs
    .filter((input) => input.checked)
    .map((input) => input.value);

  const updateCandidateSelection = () => {
    const selected = candidateInputs.filter((input) => input.checked);
    const selectedIds = currentSelectedCandidateIds();
    if (selectedCount) selectedCount.textContent = t("import.selected_count", { count: selectedIds.length });
    candidateInputs.forEach((input) => {
      const badge = input.closest("[data-candidate-id]")?.querySelector(".status-badge");
      if (badge) badge.textContent = input.checked ? t("common.selected") : t("common.not_selected");
    });
    if (toggleAll) {
      toggleAll.textContent = selected.length === candidateInputs.length
        ? t("common.deselect_all")
        : t("common.select_all");
    }
    if (confirm) {
      if (selectedIds.length === 0) {
        confirm.textContent = t("import.finish_empty");
      } else if (selectedIds.length === 1) {
        confirm.textContent = t("import.take.one");
      } else {
        confirm.textContent = tp("import.take.one", "import.take.other", selectedIds.length);
      }
    }
  };

  candidateInputs.forEach((input) => input.addEventListener("change", updateCandidateSelection));
  toggleAll?.addEventListener("click", () => {
    const selectAll = candidateInputs.some((input) => !input.checked);
    candidateInputs.forEach((input) => { input.checked = selectAll; });
    updateCandidateSelection();
  });
  confirm?.addEventListener("click", async () => {
    const selected_candidate_ids = currentSelectedCandidateIds();
    if (selected_candidate_ids.length === 0 && !window.confirm(
      t("import.finish_empty_confirm"),
    )) return;
    confirm.disabled = true;
    const originalText = confirm.textContent;
    confirm.textContent = selected_candidate_ids.length === 1
      ? t("import.taking.one")
      : t("import.taking.other");
    try {
      const result = await api(`/api/v1/imports/batches/${candidateReview.dataset.batchId}/confirm`, {
        method: "POST",
        body: JSON.stringify({ selected_candidate_ids }),
      });
      toast(result.message);
      location.assign(result.redirect);
    } catch (error) {
      showInline(candidateMessage, error.message);
      confirm.disabled = false;
      confirm.textContent = originalText;
    }
  });
  updateCandidateSelection();
}

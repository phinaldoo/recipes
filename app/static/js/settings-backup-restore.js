import { api, toast } from "./app.js";
import { locale, t } from "./i18n.js";
import { uploadFormData } from "./lib/xhr-upload.js";

async function loadSummary() {
  try {
    const data = await api("/api/v1/settings/system-summary");
    Object.entries(data.counts).forEach(([key, value]) => {
      document.querySelector(`[data-count-${key}]`)?.replaceChildren(String(value));
    });
    document.querySelector("[data-application-version]")?.replaceChildren(data.application_version);
    document.querySelector("[data-database-schema]")?.replaceChildren(data.database_schema_version);
    const storageBytes = Object.values(data.storage_bytes_by_kind).reduce((sum, value) => sum + value, 0);
    const unitBytes = storageBytes >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2;
    const storageText = new Intl.NumberFormat(locale, {
      style: "unit",
      unit: unitBytes === 1024 ** 3 ? "gigabyte" : "megabyte",
      maximumFractionDigits: 1,
    }).format(storageBytes / unitBytes);
    document.querySelector("[data-storage-total]")?.replaceChildren(storageText);
  } catch (error) {
    toast(error.message, "error");
  }
}
document.querySelector("[data-refresh-summary]")?.addEventListener("click", loadSummary);
loadSummary();

document.querySelector("[data-create-backup]")?.addEventListener("click", async (event) => {
  event.target.disabled = true;
  try {
    await api("/api/v1/settings/backups", { method: "POST" });
    toast(t("settings.backup_started"));
    location.reload();
  } catch (error) {
    toast(error.message, "error");
    event.target.disabled = false;
  }
});

document.querySelectorAll("[data-maintenance-job]").forEach((card) => {
  if (!["queued", "running"].includes(card.dataset.status)) return;
  const poll = async () => {
    try {
      const operation = card.dataset.operation === "export" ? "backups" : "restores";
      const data = await api(`/api/v1/settings/${operation}/${card.dataset.maintenanceJob}`);
      card.querySelector(".status-badge").textContent = data.job.current_stage;
      card.querySelector("progress").value = data.job.progress;
      if (["queued", "running"].includes(data.job.status)) setTimeout(poll, 2500);
      else location.reload();
    } catch {
      // A successful restore invalidates this session and removes the old job.
    }
  };
  poll();
});

const restoreForm = document.querySelector("[data-restore-upload]");
const restoreInput = restoreForm?.querySelector("input[type='file']");
const restoreDropZone = document.querySelector("[data-restore-drop-zone]");
const restoreFileSummary = document.querySelector("[data-restore-file-summary]");
const restoreFileClear = document.querySelector("[data-restore-file-clear]");
const restoreSubmit = document.querySelector("[data-restore-upload-submit]");
const restoreCancel = document.querySelector("[data-restore-upload-cancel]");
const uploadStatus = document.querySelector("[data-restore-upload-status]");
const uploadProgress = document.querySelector("[data-restore-upload-progress]");
const uploadStatusTitle = document.querySelector("[data-restore-status-title]");
const uploadStatusPercent = document.querySelector("[data-restore-status-percent]");
const uploadStatusDetail = document.querySelector("[data-restore-status-detail]");
const preflightBox = document.querySelector("[data-restore-preflight]");
const restoreDialog = document.querySelector("[data-restore-dialog]");
const confirmationForm = document.querySelector("[data-restore-confirm]");
const confirmationSubmit = document.querySelector("[data-restore-confirm-submit]");
let preflightToken = null;
let uploadController = null;

function formatBytes(bytes) {
  const unitBytes = bytes >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2;
  return new Intl.NumberFormat(locale, {
    style: "unit",
    unit: unitBytes === 1024 ** 3 ? "gigabyte" : "megabyte",
    maximumFractionDigits: 1,
  }).format(bytes / unitBytes);
}

function setUploadBusy(busy) {
  if (restoreInput) restoreInput.disabled = busy;
  if (restoreFileClear) restoreFileClear.disabled = busy;
  if (restoreSubmit) restoreSubmit.disabled = busy || !restoreInput?.files?.length;
  if (restoreCancel) restoreCancel.hidden = !busy;
  restoreDropZone?.classList.toggle("is-busy", busy);
  restoreForm?.setAttribute("aria-busy", String(busy));
}

function clearPreflight() {
  preflightToken = null;
  if (!preflightBox) return;
  preflightBox.hidden = true;
  preflightBox.className = "restore-preflight";
  preflightBox.replaceChildren();
}

function updateSelectedFile() {
  clearPreflight();
  const file = restoreInput?.files?.[0];
  if (restoreFileSummary) restoreFileSummary.hidden = !file;
  restoreDropZone?.classList.toggle("has-file", Boolean(file));
  if (file) {
    document.querySelector("[data-restore-file-name]")?.replaceChildren(file.name);
    document.querySelector("[data-restore-file-size]")?.replaceChildren(formatBytes(file.size));
  }
  if (restoreSubmit) restoreSubmit.disabled = !file;
  if (uploadStatus) uploadStatus.hidden = true;
}

function showUploadProgress({ loaded, total, percent }) {
  if (!uploadStatus || !uploadProgress) return;
  uploadStatus.hidden = false;
  uploadStatusTitle.textContent = t("settings.uploading_backup");
  if (percent === null) {
    uploadProgress.removeAttribute("value");
    uploadStatusPercent.textContent = "";
  } else {
    uploadProgress.value = percent;
    uploadStatusPercent.textContent = `${percent}%`;
  }
  uploadStatusDetail.textContent = total > 0
    ? t("settings.upload_progress", { uploaded: formatBytes(loaded), total: formatBytes(total) })
    : t("settings.upload_progress_unknown", { uploaded: formatBytes(loaded) });
}

function showVerificationStatus() {
  if (!uploadStatus || !uploadProgress) return;
  uploadStatus.hidden = false;
  if (restoreCancel) restoreCancel.hidden = true;
  uploadStatusTitle.textContent = t("settings.verifying_backup");
  uploadStatusPercent.textContent = "";
  uploadStatusDetail.textContent = t("settings.verifying_backup_detail");
  uploadProgress.removeAttribute("value");
}

function appendDefinitionList(parent, items) {
  const list = document.createElement("dl");
  list.className = "restore-summary";
  items.forEach(([label, value]) => {
    const item = document.createElement("div");
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = value;
    item.append(term, description);
    list.append(item);
  });
  parent.append(list);
}

function showPreflightSuccess(result) {
  const summary = result.job.summary;
  preflightToken = result.preflight_token;
  uploadStatus.hidden = true;
  preflightBox.hidden = false;
  preflightBox.className = "restore-preflight restore-preflight--success";
  preflightBox.replaceChildren();

  const heading = document.createElement("div");
  heading.className = "restore-preflight__heading";
  const badge = document.createElement("span");
  badge.className = "restore-preflight__check";
  badge.setAttribute("aria-hidden", "true");
  badge.textContent = "✓";
  const copy = document.createElement("div");
  const title = document.createElement("h3");
  title.tabIndex = -1;
  title.textContent = t("settings.preflight_passed");
  const intro = document.createElement("p");
  intro.textContent = t("settings.preflight_ready");
  copy.append(title, intro);
  heading.append(badge, copy);
  preflightBox.append(heading);

  const createdAt = summary.created_at
    ? new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(summary.created_at))
    : "–";
  appendDefinitionList(preflightBox, [
    [t("settings.backup_created"), createdAt],
    [t("settings.backup_version"), summary.application_version || "–"],
    [t("settings.backup_schema"), summary.source_database_schema_version || "–"],
    [t("settings.backup_contents"), t("settings.preflight_counts", {
      users: summary.counts.users || 0,
      recipes: summary.counts.recipes || 0,
      comments: summary.counts.recipe_comments || 0,
      categories: summary.counts.categories || 0,
      files: summary.media_file_count,
    })],
    [t("settings.required_storage_label"), formatBytes(summary.required_disk_bytes)],
  ]);

  if (summary.warnings?.length) {
    const warnings = document.createElement("div");
    warnings.className = "alert alert--warning";
    const warningTitle = document.createElement("strong");
    warningTitle.textContent = t("settings.preflight_warnings");
    const list = document.createElement("ul");
    summary.warnings.forEach((message) => {
      const item = document.createElement("li");
      item.textContent = message;
      list.append(item);
    });
    warnings.append(warningTitle, list);
    preflightBox.append(warnings);
  }

  const warning = document.createElement("p");
  warning.className = "restore-preflight__warning";
  warning.textContent = t("settings.safety_warning");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button button--danger";
  button.textContent = t("settings.prepare_restore");
  button.addEventListener("click", () => {
    restoreDialog.querySelector("[name='preflight_token']").value = preflightToken;
    restoreDialog.showModal();
    restoreDialog.querySelector("[name='password']")?.focus();
  });
  preflightBox.append(warning, button);
  title.focus();
}

restoreInput?.addEventListener("change", updateSelectedFile);
restoreFileClear?.addEventListener("click", () => {
  restoreInput.value = "";
  updateSelectedFile();
  restoreInput.focus();
});
restoreCancel?.addEventListener("click", () => uploadController?.abort());

restoreForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = restoreInput.files?.[0];
  if (!file) {
    restoreInput.reportValidity();
    return;
  }

  clearPreflight();
  uploadController = new AbortController();
  setUploadBusy(true);
  showUploadProgress({ loaded: 0, total: file.size, percent: 0 });
  const body = new FormData();
  body.append("file", file);
  try {
    const result = await uploadFormData("/api/v1/settings/restores/preflight", body, {
      csrfToken: document.querySelector('meta[name="csrf-token"]')?.content || "",
      signal: uploadController.signal,
      onUploadProgress: showUploadProgress,
      onUploadComplete: showVerificationStatus,
      fallbackMessage: t("error.generic"),
      networkMessage: t("settings.upload_network_error"),
      cancelledMessage: t("settings.upload_cancelled"),
    });
    showPreflightSuccess(result);
  } catch (error) {
    if (error.status === 401) {
      const next = encodeURIComponent(location.pathname + location.search);
      location.assign(`/login?reason=expired&next=${next}`);
      return;
    }
    uploadStatus.hidden = true;
    preflightBox.hidden = false;
    preflightBox.className = `restore-preflight alert alert--${error.code === "cancelled" ? "warning" : "error"}`;
    preflightBox.textContent = error.message;
  } finally {
    uploadController = null;
    setUploadBusy(false);
  }
});

function syncConfirmationButton() {
  if (!confirmationForm || !confirmationSubmit) return;
  confirmationSubmit.disabled = !confirmationForm.elements.password.value
    || confirmationForm.elements.confirmation.value !== t("settings.confirmation_word");
}
confirmationForm?.addEventListener("input", syncConfirmationButton);
confirmationForm?.addEventListener("reset", () => setTimeout(syncConfirmationButton));
restoreDialog?.addEventListener("close", () => {
  confirmationForm?.reset();
  syncConfirmationButton();
});
confirmationForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = {
    preflight_token: form.elements.preflight_token.value,
    password: form.elements.password.value,
    confirmation: form.elements.confirmation.value,
  };
  confirmationSubmit.disabled = true;
  confirmationSubmit.setAttribute("aria-busy", "true");
  confirmationSubmit.textContent = t("settings.restore_starting");
  try {
    await api("/api/v1/settings/restores", { method: "POST", body: JSON.stringify(payload) });
    restoreDialog.close();
    toast(t("settings.restore_started"));
    location.reload();
  } catch (error) {
    toast(error.message, "error");
    confirmationSubmit.removeAttribute("aria-busy");
    confirmationSubmit.textContent = t("settings.replace");
    syncConfirmationButton();
  }
});

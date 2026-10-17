import { api, showInline, toast } from "./app.js";
import { locale, t } from "./i18n.js";
import { uploadFormData } from "./lib/xhr-upload.js";
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

const workspace = document.querySelector('[data-import-workspace]');
const list = document.querySelector('[data-import-list]');
const listError = document.querySelector('[data-list-error]');
const search = document.querySelector('[data-import-search]');
const dialog = document.querySelector('[data-import-dialog]');
const panelContent = dialog.querySelector('[data-panel-content]');
const drafts = new Map();
let filter = document.querySelector('[data-import-filter][aria-pressed=true]').dataset.importFilter;
let activeBatch = null;
let listRequest = 0;
let panelRequest = 0;
let pages = 1;
let polling;
let searchTimer;
let submitting = false;
let confirming = false;
let opener = null;
let dirty = false;

async function fragment(path) {
  const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
  if (response.redirected && new URL(response.url).pathname === '/login') {
    location.assign(response.url);
    throw new Error(t('login.expired'));
  }
  if (!response.ok) throw new Error(t('workspace.network'));
  return response.text();
}

function setMode(mode, focus = false) {
  document.querySelectorAll('[data-source-tab]').forEach(button => {
    const selected = button.dataset.sourceTab === mode;
    button.setAttribute('aria-selected', String(selected));
    button.tabIndex = selected ? 0 : -1;
    if (selected && focus) button.focus();
  });
  document.querySelector('#iw-link-panel').hidden = mode !== 'link';
  document.querySelector('#iw-file-panel').hidden = mode !== 'file';
}

function addFiles(files) {
  if (submitting) return;
  const maxMB = Number(workspace.dataset.maxUpload);
  const accepted = [...files].filter(file => file.size <= maxMB * 1024 * 1024
    && /\.(jpe?g|png|webp|heic|heif|pdf)$/i.test(file.name));
  const { rejected } = importFileQueue.add(accepted);
  clearSelectionMessage();
  renderSelectedFiles();
  if (accepted.length < files.length) showInline(fileSelectionMessage, t('workspace.file_invalid', {size: maxMB}), 'warning');
  else if (rejected.length) showInline(fileSelectionMessage, t('import.file_limit', {limit: 20, count: rejected.length}), 'warning');
}
fileInput.addEventListener('change', () => { addFiles(fileInput.files); fileInput.value = ''; });
const dropZone = document.querySelector('[data-drop-zone]');
dropZone.addEventListener('dragover', event => { event.preventDefault(); dropZone.classList.add('dragging'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragging'));
dropZone.addEventListener('drop', event => { event.preventDefault(); dropZone.classList.remove('dragging'); addFiles(event.dataTransfer.files); });
window.addEventListener('pagehide', () => { revokePreviewUrls(); clearTimeout(polling); });
window.addEventListener('beforeunload', event => { if (dirty || submitting || confirming) { event.preventDefault(); event.returnValue = ''; } });
renderSelectedFiles();

function schedulePoll() { clearTimeout(polling); if (!document.hidden) polling = setTimeout(poll, 3500); }
async function refreshList({ append = false, reset = false } = {}) {
  const request = ++listRequest;
  if (reset) pages = 1;
  if (append) pages++;
  const chunks = [];
  try {
    for (let index = 0; index < pages; index++) {
      const query = new URLSearchParams({q: search.value, filter, offset: index * 30});
      chunks.push(await fragment(`/importieren/liste?${query}`));
    }
    if (request !== listRequest) return;
    const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.openImport : null;
    list.innerHTML = chunks.map((html, index) => {
      if (index === chunks.length - 1) return html;
      const doc = new DOMParser().parseFromString(html, 'text/html');
      doc.querySelector('[data-more-imports]')?.remove();
      return doc.body.innerHTML;
    }).join('');
    listError.hidden = true;
    if (focusedId) list.querySelector(`[data-open-import="${CSS.escape(focusedId)}"]`)?.focus({preventScroll: true});
  } catch (error) {
    if (request !== listRequest) return;
    if (append) pages = Math.max(1, pages - 1);
    showInline(listError, error.message);
  }
  schedulePoll();
}

function applyDrafts() {
  const draft = drafts.get(activeBatch);
  panelContent.querySelectorAll('[data-candidate-id]').forEach(card => {
    const candidate = draft?.get(card.dataset.candidateId);
    if (!candidate) return;
    const checkbox = card.querySelector('[data-candidate-select]');
    if (!checkbox) return;
    checkbox.checked = candidate.selected;
    card.querySelectorAll('[data-edit]').forEach(field => { field.value = candidate.payload[field.dataset.edit] ?? ''; });
    card.querySelectorAll('[data-ingredient-edit]').forEach(field => {
      const row = field.closest('[data-ingredient]');
      field.value = candidate.payload.ingredient_groups[Number(row.dataset.group)].ingredients[Number(row.dataset.ingredient)][field.dataset.ingredientEdit] ?? '';
    });
    card.querySelectorAll('[data-step-edit]').forEach(field => { field.value = candidate.payload.instruction_steps[Number(field.dataset.stepEdit)].text; });
  });
  updateSelection();
}
function captureDrafts() {
  const panel = panelContent.querySelector('[data-panel-batch]');
  if (!activeBatch || panel?.dataset.panelBatch !== activeBatch || panel.dataset.review !== 'true') return;
  const draft = new Map();
  panelContent.querySelectorAll('[data-candidate-id]').forEach(card => {
    const checkbox = card.querySelector('[data-candidate-select]');
    if (!checkbox) return;
    const payload = JSON.parse(card.querySelector('[data-candidate-payload]').textContent);
    card.querySelectorAll('[data-edit]').forEach(field => { payload[field.dataset.edit] = field.value; });
    card.querySelectorAll('[data-ingredient-edit]').forEach(field => {
      const row = field.closest('[data-ingredient]');
      payload.ingredient_groups[Number(row.dataset.group)].ingredients[Number(row.dataset.ingredient)][field.dataset.ingredientEdit] = field.value || null;
    });
    card.querySelectorAll('[data-step-edit]').forEach(field => { payload.instruction_steps[Number(field.dataset.stepEdit)].text = field.value; });
    draft.set(card.dataset.candidateId, {selected: checkbox.checked, payload});
  });
  drafts.set(activeBatch, draft);
}
function updateSelection() {
  const fields = [...panelContent.querySelectorAll('[data-candidate-select]')];
  const count = fields.filter(field => field.checked).length;
  const label = panelContent.querySelector('[data-selected-count]');
  const save = panelContent.querySelector('[data-save-candidates]');
  if (label) label.textContent = fields.length ? t('import.selected_count', {count}) : '';
  if (save) save.textContent = count ? t(count === 1 ? 'workspace.save_one' : 'workspace.save', {count}) : t('workspace.discard');
  const toggle = panelContent.querySelector('[data-toggle-candidates]');
  if (toggle) toggle.textContent = fields.length && count === fields.length ? t('common.deselect_all') : t('common.select_all');
}

async function openBatch(id, { push = true, refresh = false } = {}) {
  if (confirming) return;
  captureDrafts();
  const request = ++panelRequest;
  const previousBatch = activeBatch;
  activeBatch = id;
  try {
    const html = await fragment(`/importieren/${encodeURIComponent(id)}/panel`);
    if (request !== panelRequest || activeBatch !== id) return;
    panelContent.innerHTML = html;
    applyDrafts();
    if (!dialog.open) { opener = document.activeElement; dialog.showModal(); }
    if (refresh) panelContent.querySelector('[data-close-import]')?.focus({preventScroll: true});
    if (push && previousBatch !== id) history.pushState({importBatch: id}, '', `/importieren/${id}${location.search}`);
  } catch (error) {
    if (request !== panelRequest) return;
    activeBatch = previousBatch;
    showInline(dialog.open ? panelContent.querySelector('[data-panel-error]') : listError, error.message);
  }
  schedulePoll();
}
function closeBatch() {
  if (confirming) return;
  captureDrafts();
  activeBatch = null;
  ++panelRequest;
  dialog.close();
  history.replaceState(null, '', `/importieren${location.search}`);
  if (opener?.isConnected) opener.focus({preventScroll: true});
}
dialog.addEventListener('cancel', event => { event.preventDefault(); closeBatch(); });
dialog.addEventListener('close', () => {
  // app.js also closes dialogs when their backdrop is clicked.
  if (activeBatch && !confirming) closeBatch();
});
window.addEventListener('popstate', () => {
  const match = location.pathname.match(/^\/importieren\/([0-9a-f-]{36})$/);
  if (match) openBatch(match[1], {push: false}); else closeBatch();
});
function batchState(data) { return data.status + data.jobs.map(job => `|${job.id}:${job.status}${job.candidates.map(c => `:${c.id}:${c.status}`).join('')}`).join(''); }
async function poll() {
  if (document.hidden) return;
  try {
    if (activeBatch && !confirming) {
      const id = activeBatch;
      const data = await api(`/api/v1/imports/batches/${id}`);
      if (activeBatch === id) {
        const panel = panelContent.querySelector('[data-panel-state]');
        if (panel && panel.dataset.panelState !== batchState(data)) await openBatch(id, {push: false, refresh: true});
        else data.jobs.forEach(job => {
          const card = panelContent.querySelector(`[data-job-id="${CSS.escape(job.id)}"]`);
          if (card) { card.querySelector('[data-job-progress]').value = job.progress; card.querySelector('[data-job-status]').textContent = job.current_stage; }
        });
      }
    }
    if (list.querySelector('[data-batch-poll]')) await refreshList();
  } catch (error) { showInline(listError, error.message); }
  schedulePoll();
}
document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(polling); else { refreshList(); } });

async function submitted(result) {
  message.hidden = true;
  toast(result.message || t('workspace.started'));
  pages = 1; filter = 'all'; search.value = '';
  setFilter('all');
  await refreshList({reset: true});
  if (result.batch_id) await openBatch(result.batch_id);
  else if (result.recipe_id) {
    const link = document.createElement('a'); link.className = 'button button--secondary'; link.href = `/rezepte/${result.recipe_id}`; link.textContent = t('import.open_recipe');
    message.hidden = false; message.className = 'alert'; message.replaceChildren(document.createTextNode(result.message + ' '), link);
  }
}
async function submitForm(form, action) {
  if (submitting) return;
  submitting = true;
  // Lock every source control so a second source cannot race the upload.
  const controls = [...workspace.querySelectorAll('.iw-composer button, .iw-composer input, .iw-composer textarea')];
  const states = controls.map(control => control.disabled);
  controls.forEach(control => { control.disabled = true; });
  try { await action(); } catch (error) { showInline(message, error.message); }
  finally {
    controls.forEach((control, index) => { control.disabled = states[index]; });
    submitting = false; renderSelectedFiles();
    const progress = form.querySelector('[data-upload-progress]'); if (progress) progress.hidden = true;
  }
}
fileImportForm.addEventListener('submit', event => {
  event.preventDefault();
  const files = importFileQueue.files; if (!files.length) return;
  const body = new FormData(); files.forEach(file => body.append('files', file, file.name));
  const progress = fileImportForm.querySelector('[data-upload-progress]');
  submitForm(fileImportForm, async () => {
    const result = await uploadFormData('/api/v1/imports/files', body, {
      csrfToken: document.querySelector('meta[name="csrf-token"]').content,
      fallbackMessage: t('import.upload_failed'), networkMessage: t('import.server_unreachable'),
      onUploadProgress: ({percent}) => { progress.hidden = false; if (percent === null) progress.removeAttribute('value'); else progress.value = percent; },
    });
    while (importFileQueue.size) importFileQueue.remove(0);
    await submitted(result);
  });
});
document.querySelector('[data-url-import]').addEventListener('submit', event => {
  event.preventDefault();
  const form = event.currentTarget;
  const urls = form.querySelector('textarea').value.split(/\n+/).map(value => value.trim()).filter(Boolean);
  submitForm(form, async () => { const result = await api('/api/v1/imports/urls', {method: 'POST', body: JSON.stringify({urls})}); form.reset(); await submitted(result); });
});
document.querySelector('[data-json-import]').addEventListener('submit', event => {
  event.preventDefault(); const form = event.currentTarget; const body = new FormData(); body.append('file', form.querySelector('input').files[0]);
  submitForm(form, async () => { const result = await api('/api/v1/imports/json', {method: 'POST', body}); form.reset(); await submitted(result); });
});

function setFilter(value) {
  filter = value;
  document.querySelectorAll('[data-import-filter]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.importFilter === filter)));
  const query = new URLSearchParams(); if (filter !== 'all') query.set('filter', filter); if (search.value) query.set('q', search.value);
  history.replaceState(null, '', location.pathname + (query.size ? `?${query}` : ''));
}
search.addEventListener('input', () => { clearTimeout(searchTimer); ++listRequest; searchTimer = setTimeout(() => { setFilter(filter); refreshList({reset: true}); }, 250); });
document.querySelector('.iw-tabs').addEventListener('keydown', event => {
  if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault(); const current = document.querySelector('[data-source-tab][aria-selected=true]').dataset.sourceTab;
    setMode(event.key === 'Home' ? 'link' : event.key === 'End' ? 'file' : current === 'link' ? 'file' : 'link', true);
  }
});
panelContent.addEventListener('input', event => {
  if (event.target.matches('[data-edit], [data-ingredient-edit], [data-step-edit]')) { captureDrafts(); dirty = true; }
});
panelContent.addEventListener('change', event => { if (event.target.matches('[data-candidate-select]')) { captureDrafts(); updateSelection(); } });
panelContent.addEventListener('submit', async event => {
  if (!event.target.matches('[data-review-form]')) return;
  event.preventDefault(); if (confirming) return;
  const cards = [...panelContent.querySelectorAll('[data-candidate-id]')].filter(card => card.querySelector('[data-candidate-select]')?.checked);
  for (const card of cards) {
    const invalid = [...card.querySelectorAll('input:not([type=checkbox]), textarea')].find(field => !field.checkValidity());
    if (invalid) { card.querySelector('details').open = true; invalid.reportValidity(); return; }
  }
  captureDrafts();
  const updates = Object.fromEntries([...drafts.get(activeBatch)].filter(([, value]) => value.selected).map(([id, value]) => [id, value.payload]));
  const ids = Object.keys(updates);
  if (!ids.length && !window.confirm(t('import.finish_empty_confirm'))) return;
  const id = activeBatch;
  confirming = true;
  panelContent.querySelectorAll('button, input, textarea').forEach(control => { control.disabled = true; });
  try {
    const result = await api(`/api/v1/imports/batches/${id}/confirm`, {method: 'POST', body: JSON.stringify({selected_candidate_ids: ids, candidate_updates: updates})});
    drafts.delete(id); dirty = drafts.size > 0;
    confirming = false;
    // Do not capture the now-consumed form again when replacing it.
    panelContent.querySelector('[data-panel-batch]').dataset.review = 'false';
    await openBatch(id, {push: false, refresh: true});
    toast(result.message); await refreshList();
  } catch (error) {
    showInline(panelContent.querySelector('[data-panel-error]'), error.message);
    panelContent.querySelector('[data-panel-error]').scrollIntoView({block: 'nearest'});
  } finally { confirming = false; panelContent.querySelectorAll('button, input, textarea').forEach(control => { control.disabled = false; }); }
});
document.addEventListener('click', async event => {
  const target = event.target.closest('button, a[data-open-import]'); if (!target) return;
  if (target.dataset.openImport) { event.preventDefault(); return openBatch(target.dataset.openImport); }
  if (target.dataset.sourceTab) return setMode(target.dataset.sourceTab);
  if (target.hasAttribute('data-close-import')) return closeBatch();
  if (target.dataset.importFilter) { setFilter(target.dataset.importFilter); return refreshList({reset: true}); }
  if (target.hasAttribute('data-refresh-imports')) return refreshList({reset: true});
  if (target.hasAttribute('data-more-imports')) return refreshList({append: true});
  if (target.hasAttribute('data-clear-import-filters')) { search.value = ''; setFilter('all'); return refreshList({reset: true}); }
  if (target.hasAttribute('data-toggle-candidates')) {
    const fields = [...panelContent.querySelectorAll('[data-candidate-select]')]; const checked = fields.some(field => !field.checked);
    fields.forEach(field => { field.checked = checked; }); captureDrafts(); updateSelection();
  }
  if (target.matches('[data-job-retry], [data-job-cancel]')) {
    const id = activeBatch; const job = target.closest('[data-job-id]');
    target.disabled = true;
    try {
      const result = await api(`/api/v1/imports/jobs/${job.dataset.jobId}/${target.hasAttribute('data-job-retry') ? 'retry' : 'cancel'}`, {method: 'POST'});
      toast(result.message);
      if (activeBatch === id) await openBatch(id, {push: false, refresh: true});
      await refreshList();
    } catch (error) { showInline(panelContent.querySelector('[data-panel-error]'), error.message); target.disabled = false; }
  }
});
if (workspace.dataset.openBatch) openBatch(workspace.dataset.openBatch, {push: false});
schedulePoll();

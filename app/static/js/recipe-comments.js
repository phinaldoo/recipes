import { api, toast } from "./app.js";
import { locale, t } from "./i18n.js";

const root = document.querySelector("[data-recipe-id]");
const form = document.querySelector("[data-comment-form]");
const list = document.querySelector("[data-comment-list]");

function escapeText(value) { const span = document.createElement("span"); span.textContent = value; return span.textContent; }

function appendComment(comment) {
  list.querySelector("[data-comments-empty]")?.remove();
  const article = document.createElement("article");
  article.className = "comment"; article.dataset.commentId = comment.id;
  const header = document.createElement("header");
  const avatar = document.createElement("div"); avatar.className = "avatar"; avatar.setAttribute("aria-hidden", "true"); avatar.textContent = comment.author_name.slice(0, 1).toUpperCase();
  const meta = document.createElement("div"); const title = document.createElement("h3"); title.textContent = comment.author_name; const time = document.createElement("time"); time.dateTime = comment.created_at; time.textContent = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(new Date(comment.created_at)); meta.append(title, time); header.append(avatar, meta);
  const text = document.createElement("p"); text.className = "pre-wrap"; text.dataset.commentText = ""; text.textContent = comment.text;
  const actions = document.createElement("div"); actions.className = "comment__actions";
  if (comment.can_edit) { const edit = document.createElement("button"); edit.type = "button"; edit.className = "button button--text"; edit.dataset.commentEdit = ""; edit.textContent = t("common.edit"); actions.append(edit); }
  if (comment.can_delete) { const remove = document.createElement("button"); remove.type = "button"; remove.className = "button button--text danger-link"; remove.dataset.commentDelete = ""; remove.textContent = t("common.delete"); actions.append(remove); }
  article.append(header, text, actions); list.append(article);
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault(); const textarea = form.querySelector("textarea"); const text = textarea.value.trim(); if (!text) return;
  const button = form.querySelector("button[type='submit']"); button.disabled = true;
  try { const result = await api(`/api/v1/recipes/${root.dataset.recipeId}/comments`, { method: "POST", body: JSON.stringify({ text }) }); appendComment(result.comment); textarea.value = ""; toast(result.message); }
  catch (error) { toast(error.message, "error"); }
  finally { button.disabled = false; }
});

list?.addEventListener("click", async (event) => {
  const article = event.target.closest("[data-comment-id]"); if (!article) return;
  if (event.target.matches("[data-comment-delete]")) {
    if (!confirm(t("comments.delete_confirm"))) return;
    try { const result = await api(`/api/v1/recipes/${root.dataset.recipeId}/comments/${article.dataset.commentId}`, { method: "DELETE" }); article.remove(); toast(result.message); if (!list.children.length) { const empty = document.createElement("div"); empty.className = "empty-inline"; empty.dataset.commentsEmpty = ""; empty.textContent = t("comments.empty"); list.append(empty); } } catch (error) { toast(error.message, "error"); }
  }
  if (event.target.matches("[data-comment-edit]")) {
    const textNode = article.querySelector("[data-comment-text]"); const current = textNode.textContent;
    const textarea = document.createElement("textarea"); textarea.value = current; textarea.maxLength = 10000; textarea.rows = 4; textarea.setAttribute("aria-label", t("comments.edit_label")); textNode.replaceWith(textarea); textarea.focus();
    const save = event.target; save.textContent = t("common.save"); save.dataset.commentSave = ""; delete save.dataset.commentEdit;
    const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "button button--text"; cancel.textContent = t("common.cancel"); save.after(cancel);
    cancel.addEventListener("click", () => { textarea.replaceWith(textNode); save.textContent = t("common.edit"); delete save.dataset.commentSave; save.dataset.commentEdit = ""; cancel.remove(); });
  } else if (event.target.matches("[data-comment-save]")) {
    const textarea = article.querySelector("textarea"); try { const result = await api(`/api/v1/recipes/${root.dataset.recipeId}/comments/${article.dataset.commentId}`, { method: "PUT", body: JSON.stringify({ text: textarea.value }) }); const paragraph = document.createElement("p"); paragraph.className = "pre-wrap"; paragraph.dataset.commentText = ""; paragraph.textContent = result.comment.text; textarea.replaceWith(paragraph); event.target.textContent = t("common.edit"); delete event.target.dataset.commentSave; event.target.dataset.commentEdit = ""; event.target.nextElementSibling?.remove(); toast(result.message); } catch (error) { toast(error.message, "error"); }
  }
});

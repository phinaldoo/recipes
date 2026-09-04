import { ApiError, api, toast } from "./app.js";
import { locale, t, tp } from "./i18n.js";
import { copyTextToClipboard } from "./lib/clipboard.js";

function report(error, fallback) {
  toast(error instanceof ApiError ? error.message : fallback, "error");
}

export function initializeFavoriteButtons(root = document) {
  root.querySelectorAll("[data-favorite-button]:not([data-favorite-bound])").forEach(async (button) => {
    button.dataset.favoriteBound = "true";
    const recipeId = button.dataset.recipeId;
    let favorite = button.dataset.favoriteKnown === "true";
    const render = () => {
      button.setAttribute("aria-pressed", String(favorite));
      if (button.dataset.favoriteVariant === "icon") {
        const recipeTitle = button.dataset.recipeTitle || t("recipe.one");
        button.setAttribute("aria-label", t(favorite ? "recipe.favorite_remove" : "recipe.favorite_add", { title: recipeTitle }));
        button.title = t(favorite ? "recipe.favorite_remove_short" : "recipe.favorite_add_short");
        return;
      }
      button.textContent = favorite ? `★ ${t("favorites.title")}` : `☆ ${t("recipe.favorite_add_short")}`;
      if (document.body.dataset.page === "favorites" && favorite) {
        button.textContent = `★ ${t("recipe.favorite_remove_short")}`;
      }
    };
    if (!button.dataset.favoriteKnown) {
      try {
        favorite = (await api(`/api/v1/favorites/${recipeId}`)).favorite;
      } catch {
        return;
      }
    }
    render();
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      try {
        const result = await api(`/api/v1/favorites/${recipeId}`, {
          method: favorite ? "DELETE" : "PUT",
        });
        favorite = result.favorite;
        render();
        toast(result.message);
        if (!favorite && document.body.dataset.page === "favorites") {
          button.closest(".recipe-card")?.remove();
          if (!document.querySelector(".recipe-card")) location.reload();
        }
      } catch (error) {
        report(error, t("productivity.favorite_error"));
      } finally {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      }
    });
  });
}

initializeFavoriteButtons();
document.addEventListener("recipe-results:updated", (event) => {
  initializeFavoriteButtons(event.target);
});

document.querySelector("[data-tag-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  try {
    await api("/api/v1/tags", {
      method: "POST",
      body: JSON.stringify({ name: new FormData(form).get("name") }),
    });
    location.reload();
  } catch (error) {
    report(error, t("productivity.tag_create_error"));
  }
});

document.querySelectorAll("[data-tag-id]").forEach((row) => {
  row.querySelector("[data-tag-rename]")?.addEventListener("click", async () => {
    const current = row.querySelector("strong").textContent.trim();
    const name = prompt(t("productivity.tag_new_name"), current)?.trim();
    if (!name || name === current) return;
    try {
      await api(`/api/v1/tags/${row.dataset.tagId}`, {
        method: "PUT",
        body: JSON.stringify({ name }),
      });
      location.reload();
    } catch (error) {
      report(error, t("productivity.tag_rename_error"));
    }
  });
  row.querySelector("[data-tag-delete]")?.addEventListener("click", async () => {
    if (!confirm(t("productivity.tag_delete_confirm"))) return;
    try {
      await api(`/api/v1/tags/${row.dataset.tagId}`, { method: "DELETE" });
      row.remove();
      toast(t("productivity.tag_deleted"));
    } catch (error) {
      report(error, t("productivity.tag_delete_error"));
    }
  });
});

document.querySelector("[data-synonym-form]")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  try {
    await api("/api/v1/search-synonyms", {
      method: "POST",
      body: JSON.stringify({ term: data.get("term"), synonym: data.get("synonym") }),
    });
    location.reload();
  } catch (error) {
    report(error, t("productivity.synonym_create_error"));
  }
});

document.querySelectorAll("[data-synonym-id]").forEach((row) => {
  row.querySelector("[data-synonym-delete]")?.addEventListener("click", async () => {
    try {
      await api(`/api/v1/search-synonyms/${row.dataset.synonymId}`, { method: "DELETE" });
      row.remove();
      toast(t("productivity.synonym_deleted"));
    } catch (error) {
      report(error, t("productivity.synonym_delete_error"));
    }
  });
});

const shareForm = document.querySelector("[data-share-form]");

function formatShareDate(value) {
  return new Intl.DateTimeFormat(locale, {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(value));
}

function bindShareRevoke(row) {
  row.querySelector("[data-share-revoke]")?.addEventListener("click", async () => {
    if (!confirm(t("share.revoke_confirm"))) return;
    try {
      await api(`/api/v1/recipes/${shareForm.dataset.recipeId}/shares/${row.dataset.shareId}`, {
        method: "DELETE",
      });
      location.reload();
    } catch (error) {
      report(error, t("share.revoke_error"));
    }
  });
}

function appendShareRow(share) {
  const list = document.querySelector("[data-share-list]");
  if (!list) return;
  list.querySelector(".empty-inline")?.remove();

  const row = document.createElement("li");
  row.className = "share-list__item";
  row.dataset.shareId = share.id;
  const details = document.createElement("span");
  details.className = "management-list__copy";
  const title = document.createElement("strong");
  title.textContent = t("share.link", { prefix: share.token_prefix });
  const metadata = document.createElement("small");
  metadata.textContent = `${t("share.created", { date: formatShareDate(share.created_at) })} · ${
    share.expires_at
      ? t("share.valid_until", { date: formatShareDate(share.expires_at) })
      : t("share.no_expiry")
  }`;
  details.append(title, metadata);

  const revoke = document.createElement("button");
  revoke.className = "button button--text danger-link";
  revoke.type = "button";
  revoke.dataset.shareRevoke = "";
  revoke.textContent = t("share.revoke");
  row.append(details, revoke);
  list.prepend(row);
  const count = document.querySelector("[data-share-count]");
  if (count) {
    const total = list.querySelectorAll("[data-share-id]").length;
    count.textContent = `${total} ${tp("share.one", "share.other", total)}`;
  }
  bindShareRevoke(row);
}

shareForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const value = new FormData(shareForm).get("expires_in_days");
  const submit = shareForm.querySelector('[type="submit"]');
  submit.disabled = true;
  submit.setAttribute("aria-busy", "true");
  try {
    const result = await api(`/api/v1/recipes/${shareForm.dataset.recipeId}/shares`, {
      method: "POST",
      body: JSON.stringify({ expires_in_days: value ? Number.parseInt(value, 10) : null }),
    });
    const panel = document.querySelector("[data-share-result]");
    panel.hidden = false;
    panel.querySelector("[data-share-url]").value = result.url;
    appendShareRow(result.share);
    toast(result.message, "info");
  } catch (error) {
    report(error, t("share.create_error"));
  } finally {
    submit.disabled = false;
    submit.removeAttribute("aria-busy");
  }
});

document.querySelector("[data-copy-share]")?.addEventListener("click", async () => {
  const input = document.querySelector("[data-share-url]");
  try {
    await copyTextToClipboard(input.value);
    toast(t("share.copied"));
  } catch {
    toast(t("share.copy_error"), "error");
  }
});

document.querySelectorAll("[data-share-id]").forEach(bindShareRevoke);

const history = document.querySelector("[data-version-history]");
history?.querySelectorAll("[data-version-restore]").forEach((button) => {
  button.addEventListener("click", async () => {
    if (!confirm(t("history.restore_confirm"))) return;
    button.disabled = true;
    try {
      const result = await api(
        `/api/v1/recipes/${history.dataset.recipeId}/versions/${button.dataset.versionRestore}/restore`,
        {
          method: "POST",
          body: JSON.stringify({ expected_updated_at: history.dataset.expectedUpdatedAt }),
        },
      );
      sessionStorage.setItem("rezepte-flash", JSON.stringify({ message: result.message }));
      location.assign(result.redirect);
    } catch (error) {
      report(error, t("history.restore_error"));
      button.disabled = false;
    }
  });
});

import { api, toast } from "./app.js";
import { locale, t } from "./i18n.js";

const form = document.querySelector("[data-recipe-search]");
const query = form?.querySelector("#recipe-query");
const HISTORY_STATE_KEY = "recipeStream";
let activeRequest;
let activeAppendRequest;
let debounceTimer;
let infiniteObserver;
let nextHistoryMode = "replace";

function currentResultsRegion() {
  return document.querySelector("[data-recipe-results-region]");
}

function positiveInteger(value, fallback = 0) {
  const parsed = Number.parseInt(value || "", 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
}

function searchUrlFromForm() {
  const url = new URL(form.action, location.href);
  const parameters = new URLSearchParams();
  for (const [name, value] of new FormData(form)) {
    if (typeof value === "string" && value !== "") parameters.append(name, value);
  }
  parameters.delete("page");
  url.search = parameters.toString();
  return url;
}

function recipeUrlKey(value = location.href) {
  const url = new URL(value, location.href);
  url.searchParams.delete("page");
  url.hash = "";
  return `${url.pathname}${url.search}`;
}

function historyStateObject() {
  return history.state && typeof history.state === "object" ? { ...history.state } : {};
}

function storedRecipeState(url = location.href) {
  const state = history.state?.[HISTORY_STATE_KEY];
  if (!state || state.key !== recipeUrlKey(url)) return null;
  return {
    key: state.key,
    loadedPage: positiveInteger(state.loadedPage, 1) || 1,
    scrollY: positiveInteger(state.scrollY),
  };
}

function replaceRecipeState({ loadedPage, scrollY = window.scrollY }, url = location.href) {
  try {
    const state = historyStateObject();
    state[HISTORY_STATE_KEY] = {
      key: recipeUrlKey(url),
      loadedPage: Math.max(1, loadedPage),
      scrollY: Math.max(0, Math.round(scrollY)),
    };
    history.replaceState(state, "", url);
  } catch {
    // Search and infinite loading do not depend on history-state persistence.
  }
}

function persistCurrentRecipeState() {
  const stream = currentResultsRegion()?.querySelector("[data-recipe-stream]");
  replaceRecipeState({
    loadedPage: positiveInteger(stream?.dataset.page, 1) || 1,
    scrollY: window.scrollY,
  });
}

function updateHistory(url, mode) {
  if (mode === "none") return;

  const state = historyStateObject();
  state[HISTORY_STATE_KEY] = {
    key: recipeUrlKey(url),
    loadedPage: 1,
    scrollY: 0,
  };
  try {
    history[mode === "push" ? "pushState" : "replaceState"](state, "", url);
  } catch {
    // A blocked History API should not prevent the results from updating.
  }
}

function updateClearSearchLink() {
  const clear = form?.querySelector("[data-recipe-search-clear]");
  if (!clear || !query) return;
  clear.hidden = query.value.length === 0;
  const url = searchUrlFromForm();
  url.searchParams.delete("q");
  url.searchParams.delete("page");
  clear.href = url.href;
}

function updateRecipeKindPresentation(selectedRecipeKind) {
  const kind = ["cooking", "baking"].includes(selectedRecipeKind) ? selectedRecipeKind : "";
  const heading = document.querySelector("[data-recipe-kind-heading]");
  const headingText = t(kind ? `recipes.kind.${kind}` : "recipes.title");
  if (heading) heading.textContent = headingText;
  document.title = kind ? `${headingText} · ${t("recipes.title")}` : headingText;
}

function syncFormWithUrl(url) {
  if (!form || !query) return;
  query.value = url.searchParams.get("q") || "";

  const selectedRecipeKind = url.searchParams.get("recipe_kind") || "";
  form.querySelectorAll('input[name="recipe_kind"]').forEach((input) => {
    input.checked = input.value === selectedRecipeKind;
  });
  updateRecipeKindPresentation(selectedRecipeKind);

  const selectedCategories = new Set(url.searchParams.getAll("category_ids"));
  const categoryInputs = form.querySelectorAll('input[name="category_ids"]');
  categoryInputs.forEach((input) => {
    input.checked = selectedCategories.has(input.value);
  });

  const sort = form.querySelector('select[name="sort"]');
  if (sort) sort.value = url.searchParams.get("sort") || "updated_desc";

  const summary = form.querySelector("[data-recipe-filter-summary]");
  if (summary) {
    const selectedCount = [...categoryInputs].filter((input) => input.checked).length;
    summary.textContent = selectedCount ? `${t("recipes.filter")} · ${selectedCount}` : t("recipes.filter");
  }
  updateClearSearchLink();
}

function fullNavigation(url) {
  try {
    sessionStorage.setItem("recipe-search-focus", "1");
  } catch {
    // Focus restoration is optional when browser storage is unavailable.
  }
  location.assign(url.href);
}

function loginNavigation() {
  const next = encodeURIComponent(location.pathname + location.search);
  location.assign(`/login?reason=expired&next=${next}`);
}

function setSearchLoading(region, loading) {
  if (!region) return;
  const skeleton = region.querySelector("[data-recipe-search-skeleton]");
  const results = region.querySelector("[data-recipe-results]");
  const summary = region.querySelector("[data-recipe-results-summary]");
  region.setAttribute("aria-busy", String(loading));
  if (skeleton) skeleton.hidden = !loading;
  if (results) results.hidden = loading;
  if (summary) {
    if (loading) {
      summary.dataset.previousText = summary.textContent || "";
      summary.textContent = t("recipes.loading_results");
    } else if (summary.dataset.previousText !== undefined) {
      summary.textContent = summary.dataset.previousText;
      delete summary.dataset.previousText;
    }
  }
}

function bindRestoreButtons(root = document) {
  root.querySelectorAll("[data-restore-recipe]:not([data-restore-bound])").forEach((button) => {
    button.dataset.restoreBound = "true";
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const result = await api(`/api/v1/recipes/${button.dataset.restoreRecipe}/restore`, {
          method: "POST",
        });
        toast(result.message);
        await loadResults(new URL(location.href), { historyMode: "none" });
      } catch (error) {
        button.disabled = false;
        toast(error.message, "error");
      }
    });
  });
}

function disconnectInfiniteObserver() {
  infiniteObserver?.disconnect();
  infiniteObserver = undefined;
}

function streamParts(stream) {
  return {
    grid: stream.querySelector("[data-recipe-grid]"),
    skeletons: stream.querySelector("[data-recipe-stream-skeletons]"),
    error: stream.querySelector("[data-recipe-stream-error]"),
    retry: stream.querySelector("[data-recipe-stream-retry]"),
    more: stream.querySelector("[data-recipe-stream-more]"),
    sentinel: stream.querySelector("[data-recipe-stream-sentinel]"),
    end: stream.querySelector("[data-recipe-stream-end]"),
    status: stream.querySelector("[data-recipe-stream-status]"),
  };
}

function setNextUrl(stream, nextUrl) {
  const { more, sentinel } = streamParts(stream);
  if (nextUrl) {
    stream.dataset.nextUrl = nextUrl;
    if (more) {
      more.href = new URL(nextUrl, location.href).href;
      more.hidden = false;
    }
    if (sentinel) sentinel.hidden = false;
  } else {
    delete stream.dataset.nextUrl;
    if (more) more.hidden = true;
    if (sentinel) sentinel.hidden = true;
  }
}

function finishStreamLoading(stream) {
  const { skeletons } = streamParts(stream);
  stream.setAttribute("aria-busy", "false");
  if (skeletons) skeletons.hidden = true;
}

function completeStream(stream, { announce = true, revealEnd = true } = {}) {
  const { end, error, status } = streamParts(stream);
  setNextUrl(stream, "");
  if (error) error.hidden = true;
  if (end) end.hidden = !revealEnd;
  if (announce && status) {
    status.textContent = t("recipes.all_loaded", {
      total: positiveInteger(stream.dataset.total),
    });
  }
  disconnectInfiniteObserver();
}

async function loadNextBatch(stream, { announce = true, persist = true } = {}) {
  if (!stream?.isConnected || activeAppendRequest || !stream.dataset.nextUrl) return false;

  const url = new URL(stream.dataset.nextUrl, location.href);
  const action = new URL(form.action, location.href);
  if (url.origin !== action.origin || url.pathname !== action.pathname) return false;

  const controller = new AbortController();
  activeAppendRequest = controller;
  const { skeletons, error, more, sentinel, end, status } = streamParts(stream);
  stream.setAttribute("aria-busy", "true");
  if (skeletons) skeletons.hidden = false;
  if (error) error.hidden = true;
  if (more) more.hidden = true;
  if (sentinel) sentinel.hidden = true;
  if (end) end.hidden = true;
  if (announce && status) status.textContent = t("recipes.loading_more");

  try {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: {
        Accept: "text/html",
        "X-Recipe-Results": "append",
      },
      signal: controller.signal,
    });
    if (response.redirected) {
      location.assign(response.url);
      return false;
    }
    if (response.status === 401) {
      loginNavigation();
      return false;
    }
    if (!response.ok) throw new Error(`Recipe batch failed with status ${response.status}`);

    const html = await response.text();
    const documentFragment = new DOMParser().parseFromString(html, "text/html");
    const batch = documentFragment.querySelector("[data-recipe-batch]");
    const { grid } = streamParts(stream);
    if (!batch || !grid || !stream.isConnected) {
      throw new Error("Recipe batch response is incomplete");
    }

    const existingIds = new Set(
      [...grid.querySelectorAll("[data-recipe-card]")].map((card) => card.dataset.recipeId),
    );
    const appendedCards = [];
    const cards = [...batch.querySelectorAll(":scope > [data-recipe-card]")];
    const fragment = document.createDocumentFragment();
    for (const card of cards) {
      if (card.dataset.recipeId && existingIds.has(card.dataset.recipeId)) continue;
      if (card.dataset.recipeId) existingIds.add(card.dataset.recipeId);
      card.classList.add("recipe-card--new");
      appendedCards.push(card);
      fragment.append(card);
    }
    grid.append(fragment);

    const returnedPage = positiveInteger(batch.dataset.page, positiveInteger(stream.dataset.page, 1));
    stream.dataset.page = String(Math.max(positiveInteger(stream.dataset.page, 1), returnedPage));
    stream.dataset.pages = String(positiveInteger(batch.dataset.pages, returnedPage));
    stream.dataset.total = String(positiveInteger(batch.dataset.total, existingIds.size));
    stream.dataset.loaded = String(grid.querySelectorAll("[data-recipe-card]").length);
    setNextUrl(stream, batch.dataset.nextUrl || "");

    bindRestoreButtons(stream);
    if (appendedCards.length) {
      stream.dispatchEvent(new CustomEvent("recipe-results:updated", {
        bubbles: true,
        detail: { appended: true, count: appendedCards.length },
      }));
      window.setTimeout(() => {
        appendedCards.forEach((card) => card.classList.remove("recipe-card--new"));
      }, 400);
    }

    const loaded = positiveInteger(stream.dataset.loaded);
    const total = positiveInteger(stream.dataset.total);
    if (stream.dataset.nextUrl) {
      if (announce && status) status.textContent = t("recipes.loaded_count", { loaded, total });
    } else {
      completeStream(stream, { announce, revealEnd: true });
    }
    if (persist) replaceRecipeState({ loadedPage: returnedPage, scrollY: window.scrollY });
    return true;
  } catch (errorValue) {
    if (errorValue instanceof DOMException && errorValue.name === "AbortError") {
      setNextUrl(stream, stream.dataset.nextUrl || "");
      return false;
    }
    if (stream.isConnected) {
      if (error) error.hidden = false;
      if (status) status.textContent = t("recipes.load_error");
      if (more) more.hidden = true;
      if (sentinel) sentinel.hidden = true;
    }
    return false;
  } finally {
    if (activeAppendRequest === controller) activeAppendRequest = undefined;
    if (stream.isConnected) finishStreamLoading(stream);
  }
}

function observeStream(stream) {
  disconnectInfiniteObserver();
  if (!("IntersectionObserver" in window) || !stream.dataset.nextUrl) return;
  const { sentinel } = streamParts(stream);
  if (!sentinel) return;

  stream.classList.add("recipe-stream--auto");
  sentinel.hidden = false;
  infiniteObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) void loadNextBatch(stream);
    },
    { rootMargin: "700px 0px" },
  );
  infiniteObserver.observe(sentinel);
}

async function initializeRecipeStream(root = document, restoreState = null) {
  disconnectInfiniteObserver();
  const stream = root.querySelector("[data-recipe-stream]");
  if (!stream) return;
  const { more, retry } = streamParts(stream);

  more?.addEventListener("click", (event) => {
    event.preventDefault();
    void loadNextBatch(stream);
  });
  retry?.addEventListener("click", () => {
    void loadNextBatch(stream);
  });

  const supportsAutomaticLoading = "IntersectionObserver" in window;
  if (supportsAutomaticLoading) stream.classList.add("recipe-stream--auto");

  const targetPage = restoreState?.key === recipeUrlKey()
    ? Math.max(1, positiveInteger(restoreState.loadedPage, 1))
    : 1;
  while (
    stream.isConnected
    && stream.dataset.nextUrl
    && positiveInteger(stream.dataset.page, 1) < targetPage
  ) {
    const loaded = await loadNextBatch(stream, { announce: false, persist: false });
    if (!loaded) break;
  }

  observeStream(stream);
  if (restoreState?.key === recipeUrlKey() && restoreState.scrollY > 0) {
    const restoreScroll = () => window.scrollTo({
      top: restoreState.scrollY,
      behavior: "auto",
    });
    restoreScroll();
    requestAnimationFrame(() => {
      requestAnimationFrame(restoreScroll);
    });
    window.setTimeout(restoreScroll, 100);
  }
}

async function loadResults(
  url,
  { historyMode = "replace", closeFilters = false, restoreState = null } = {},
) {
  if (!form || !query) return false;
  if (historyMode === "push") persistCurrentRecipeState();
  activeRequest?.abort();
  activeAppendRequest?.abort();
  disconnectInfiniteObserver();
  const controller = new AbortController();
  activeRequest = controller;
  const previousRegion = currentResultsRegion();
  setSearchLoading(previousRegion, true);
  form.setAttribute("aria-busy", "true");

  try {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: {
        Accept: "text/html",
        "X-Recipe-Results": "1",
      },
      signal: controller.signal,
    });
    if (response.redirected) {
      location.assign(response.url);
      return false;
    }
    if (response.status === 401) {
      loginNavigation();
      return false;
    }
    if (!response.ok) throw new Error(`Recipe search failed with status ${response.status}`);

    const html = await response.text();
    const documentFragment = new DOMParser().parseFromString(html, "text/html");
    const nextRegion = documentFragment.querySelector("[data-recipe-results-region]");
    if (!nextRegion || !previousRegion) throw new Error("Recipe search response is incomplete");

    previousRegion.replaceWith(nextRegion);
    syncFormWithUrl(url);
    if (closeFilters) form.querySelector(".filter-popover")?.removeAttribute("open");
    updateHistory(url, historyMode);
    if (historyMode === "none" && !restoreState) {
      replaceRecipeState({ loadedPage: 1, scrollY: window.scrollY }, url);
    }

    bindRestoreButtons(nextRegion);
    nextRegion.dispatchEvent(new CustomEvent("recipe-results:updated", { bubbles: true }));
    await initializeRecipeStream(nextRegion, restoreState);
    return true;
  } catch (errorValue) {
    if (errorValue instanceof DOMException && errorValue.name === "AbortError") return false;
    fullNavigation(url);
    return false;
  } finally {
    if (activeRequest === controller) {
      activeRequest = undefined;
      setSearchLoading(currentResultsRegion(), false);
      form.removeAttribute("aria-busy");
    }
  }
}

if (form && query) {
  query.addEventListener("input", () => {
    activeRequest?.abort();
    activeAppendRequest?.abort();
    clearTimeout(debounceTimer);
    updateClearSearchLink();
    debounceTimer = setTimeout(() => {
      nextHistoryMode = "replace";
      form.requestSubmit();
    }, 275);
  });

  form.addEventListener(
    "change",
    (event) => {
      if (event.target instanceof Element && event.target.matches("[data-auto-submit]")) {
        nextHistoryMode = "push";
      }
    },
    true,
  );

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    clearTimeout(debounceTimer);
    const historyMode = event.submitter ? "push" : nextHistoryMode;
    nextHistoryMode = "replace";
    void loadResults(searchUrlFromForm(), {
      historyMode,
      closeFilters: Boolean(event.submitter?.closest(".filter-panel")),
    });
  });

  document.addEventListener("click", (event) => {
    if (
      event.defaultPrevented
      || event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) return;
    const clickedElement = event.target instanceof Element ? event.target : null;
    const link = clickedElement?.closest("[data-recipe-search-link]");
    if (!link || link.target || link.hasAttribute("download")) return;
    const url = new URL(link.href, location.href);
    const action = new URL(form.action, location.href);
    if (url.origin !== action.origin || url.pathname !== action.pathname) return;

    event.preventDefault();
    clearTimeout(debounceTimer);
    syncFormWithUrl(url);
    void loadResults(url, {
      historyMode: "push",
      closeFilters: Boolean(link.closest(".filter-panel")),
    });
  });

  window.addEventListener("popstate", () => {
    const url = new URL(location.href);
    const action = new URL(form.action, location.href);
    if (url.origin !== action.origin || url.pathname !== action.pathname) return;
    clearTimeout(debounceTimer);
    syncFormWithUrl(url);
    void loadResults(url, {
      historyMode: "none",
      closeFilters: true,
      restoreState: storedRecipeState(url),
    });
  });

  window.addEventListener("pagehide", persistCurrentRecipeState);

  try {
    if (sessionStorage.getItem("recipe-search-focus") === "1") {
      sessionStorage.removeItem("recipe-search-focus");
      query.focus();
      query.setSelectionRange(query.value.length, query.value.length);
    }
  } catch {
    // Focus restoration is optional when browser storage is unavailable.
  }

  updateClearSearchLink();
  bindRestoreButtons();
  const initialState = storedRecipeState();
  if (!initialState) {
    const stream = currentResultsRegion()?.querySelector("[data-recipe-stream]");
    replaceRecipeState({
      loadedPage: positiveInteger(stream?.dataset.page, 1) || 1,
      scrollY: window.scrollY,
    });
  }
  void initializeRecipeStream(document, initialState);
}

function normalizeSearchValue(value) {
  return value
    .toLocaleLowerCase(locale)
    .normalize("NFD")
    .replace(/\p{Diacritic}/gu, "")
    .replace(/ß/g, "ss");
}

document.querySelectorAll("[data-category-filter]").forEach((input) => {
  const panel = input.closest(".filter-panel");
  const categoryRows = [...panel.querySelectorAll("[data-category-name]")];
  const emptyState = panel.querySelector("[data-category-filter-empty]");

  const filterCategories = () => {
    const searchValue = normalizeSearchValue(input.value.trim());
    let visibleCount = 0;

    categoryRows.forEach((row) => {
      const matches = normalizeSearchValue(row.dataset.categoryName).includes(searchValue);
      row.hidden = !matches;
      if (matches) visibleCount += 1;
    });

    emptyState.hidden = visibleCount !== 0;
  };

  input.addEventListener("input", filterCategories);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") event.preventDefault();
  });
});

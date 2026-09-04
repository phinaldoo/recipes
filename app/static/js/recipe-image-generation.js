import { ApiError, api, toast } from "./app.js";
import { t } from "./i18n.js";

const recipe = document.querySelector("[data-recipe-id]");
const generation = document.querySelector("[data-image-generation]");
const button = generation?.querySelector("[data-generate-recipe-image]");

if (recipe && generation && button) {
  const label = button.querySelector("[data-generation-label]");
  const spinner = button.querySelector("[data-generation-spinner]");
  const available = generation.dataset.generationAvailable === "true";
  let jobId = generation.dataset.jobId || "";
  let generationMode = generation.dataset.generationMode || "create";
  let retryDelay = 1500;
  let pollTimer;

  function idleLabel() {
    return generationMode === "regenerate"
      ? t("recipe.image.regenerate")
      : t("recipe.image.create");
  }

  function setBusy(busy) {
    button.disabled = busy || !available;
    button.setAttribute("aria-busy", String(busy));
    if (spinner) spinner.hidden = !busy;
    if (label) {
      label.textContent = busy
        ? t("recipe.image.generating")
        : available
          ? idleLabel()
          : t("recipe.image.unavailable");
    }
  }

  function flash(message, type = "success") {
    try {
      sessionStorage.setItem("rezepte-flash", JSON.stringify({ message, type }));
    } catch {
      // Reloading still reveals the generated image when browser storage is unavailable.
    }
  }

  function schedulePoll(delay = retryDelay) {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, delay);
  }

  async function poll() {
    if (!jobId) return;
    try {
      const response = await api(
        `/api/v1/recipes/${recipe.dataset.recipeId}/image-generation/${jobId}`,
      );
      const job = response.job;
      generationMode = job.generation_mode || generationMode;
      generation.dataset.generationMode = generationMode;
      retryDelay = Number(job.poll_after_ms) || 1500;
      if (job.status === "completed") {
        flash(
          generationMode === "regenerate"
            ? t("recipe.image.replaced")
            : t("recipe.image.created"),
        );
        location.reload();
        return;
      }
      if (job.status === "cancelled") {
        flash(job.current_stage || t("recipe.image.ended"), "info");
        location.reload();
        return;
      }
      if (job.status === "failed") {
        jobId = "";
        generation.dataset.jobId = "";
        setBusy(false);
        const message = job.error_message || t("recipe.image.failed");
        toast(message, "error");
        return;
      }
      schedulePoll();
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        jobId = "";
        generation.dataset.jobId = "";
        setBusy(false);
        toast(t("recipe.image.job_missing"), "error");
        return;
      }
      retryDelay = Math.min(retryDelay * 2, 10_000);
      schedulePoll();
    }
  }

  button.addEventListener("click", async () => {
    if (jobId || !available) return;
    setBusy(true);
    try {
      const response = await api(
        `/api/v1/recipes/${recipe.dataset.recipeId}/image-generation`,
        { method: "POST" },
      );
      jobId = response.job.id;
      generationMode = response.job.generation_mode || generationMode;
      generation.dataset.jobId = jobId;
      generation.dataset.generationMode = generationMode;
      button.closest("details")?.removeAttribute("open");
      toast(response.message || t("recipe.image.started"), "info");
      schedulePoll(300);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        location.reload();
        return;
      }
      jobId = "";
      setBusy(false);
      const message = error?.message || t("recipe.image.start_failed");
      toast(message, "error");
    }
  });

  if (jobId) {
    setBusy(true);
    schedulePoll(300);
  } else {
    setBusy(false);
  }
}

function copyWithLegacyClipboard(text, documentObject) {
  if (
    !documentObject?.body ||
    typeof documentObject.createElement !== "function" ||
    typeof documentObject.execCommand !== "function"
  ) {
    throw new Error("Clipboard access is unavailable.");
  }

  const previouslyFocused = documentObject.activeElement;
  const textarea = documentObject.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.tabIndex = -1;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.cssText = [
    "position:fixed",
    "left:0",
    "top:0",
    "width:1px",
    "height:1px",
    "padding:0",
    "border:0",
    "opacity:0",
    "pointer-events:none",
  ].join(";");
  documentObject.body.append(textarea);

  try {
    textarea.focus({ preventScroll: true });
    textarea.select();
    textarea.setSelectionRange?.(0, textarea.value.length);
    if (!documentObject.execCommand("copy")) {
      throw new Error("Clipboard copy was rejected.");
    }
  } finally {
    textarea.remove();
    if (typeof previouslyFocused?.focus === "function") {
      try {
        previouslyFocused.focus({ preventScroll: true });
      } catch {
        previouslyFocused.focus();
      }
    }
  }
}

export async function copyTextToClipboard(
  text,
  {
    clipboard = globalThis.navigator?.clipboard,
    documentObject = globalThis.document,
    secureContext = globalThis.isSecureContext === true,
  } = {},
) {
  if (secureContext && typeof clipboard?.writeText === "function") {
    try {
      await clipboard.writeText(text);
      return;
    } catch {
      // Fall through for browsers that expose the API but deny clipboard access.
    }
  }

  copyWithLegacyClipboard(text, documentObject);
}

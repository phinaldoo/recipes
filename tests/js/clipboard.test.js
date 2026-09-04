import assert from "node:assert/strict";
import test from "node:test";

import { copyTextToClipboard } from "../../app/static/js/lib/clipboard.js";

function legacyClipboardDocument({ copyResult = true } = {}) {
  const calls = [];
  const focusedElement = {
    focus(options) {
      calls.push(["restore-focus", options]);
    },
  };
  const textarea = {
    style: {},
    setAttribute(name, value) {
      calls.push(["attribute", name, value]);
    },
    focus(options) {
      calls.push(["textarea-focus", options]);
    },
    select() {
      calls.push(["textarea-select"]);
    },
    setSelectionRange(start, end) {
      calls.push(["selection-range", start, end]);
    },
    remove() {
      calls.push(["remove"]);
    },
  };
  const documentObject = {
    activeElement: focusedElement,
    body: {
      append(element) {
        calls.push(["append", element]);
      },
    },
    createElement(tagName) {
      calls.push(["create", tagName]);
      return textarea;
    },
    execCommand(command) {
      calls.push(["command", command]);
      return copyResult;
    },
  };
  return { calls, documentObject, textarea };
}

test("secure pages use the Clipboard API", async () => {
  const writes = [];
  await copyTextToClipboard("https://example.test/share", {
    clipboard: { writeText: async (text) => writes.push(text) },
    documentObject: null,
    secureContext: true,
  });

  assert.deepEqual(writes, ["https://example.test/share"]);
});

test("insecure pages copy through a hidden textarea without touching the displayed field", async () => {
  const { calls, documentObject, textarea } = legacyClipboardDocument();
  await copyTextToClipboard("http://192.168.1.10/share/token", {
    clipboard: { writeText: async () => assert.fail("secure Clipboard API should not be used") },
    documentObject,
    secureContext: false,
  });

  assert.equal(textarea.value, "http://192.168.1.10/share/token");
  assert.equal(textarea.readOnly, true);
  assert.deepEqual(
    calls.filter(([name]) => ["create", "textarea-select", "command", "remove"].includes(name)),
    [["create", "textarea"], ["textarea-select"], ["command", "copy"], ["remove"]],
  );
  assert.equal(calls.at(-1)[0], "restore-focus");
});

test("a denied Clipboard API attempt falls back and reports a rejected legacy copy", async () => {
  const { calls, documentObject } = legacyClipboardDocument({ copyResult: false });

  await assert.rejects(
    copyTextToClipboard("https://example.test/share", {
      clipboard: { writeText: async () => Promise.reject(new Error("denied")) },
      documentObject,
      secureContext: true,
    }),
    /Clipboard copy was rejected/,
  );
  assert.ok(calls.some(([name, command]) => name === "command" && command === "copy"));
  assert.ok(calls.some(([name]) => name === "remove"));
});

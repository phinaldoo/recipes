import assert from "node:assert/strict";
import test from "node:test";

test("clicking outside closes open dropdowns and keeps the clicked dropdown open", async (context) => {
  const originalDescriptors = new Map(
    ["document", "location", "matchMedia", "navigator", "Node", "sessionStorage", "window"].map(
      (name) => [name, Object.getOwnPropertyDescriptor(globalThis, name)],
    ),
  );
  context.after(() => {
    originalDescriptors.forEach((descriptor, name) => {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    });
  });

  class FakeNode {}
  const clickedTarget = new FakeNode();
  const outsideTarget = new FakeNode();
  const insideDropdown = {
    closeCount: 0,
    contains: (target) => target === clickedTarget,
    removeAttribute(name) {
      assert.equal(name, "open");
      this.closeCount += 1;
    },
  };
  const otherDropdown = {
    closeCount: 0,
    contains: () => false,
    removeAttribute(name) {
      assert.equal(name, "open");
      this.closeCount += 1;
    },
  };
  const listeners = new Map();
  const documentStub = {
    documentElement: { classList: { toggle() {} } },
    addEventListener(name, listener) {
      listeners.set(name, listener);
    },
    querySelector() {
      return null;
    },
    querySelectorAll(selector) {
      return selector === "details[data-dropdown][open]"
        ? [insideDropdown, otherDropdown]
        : [];
    },
  };

  Object.defineProperties(globalThis, {
    document: { configurable: true, value: documentStub },
    location: { configurable: true, value: { protocol: "http:" } },
    matchMedia: { configurable: true, value: () => ({ matches: false }) },
    navigator: { configurable: true, value: {} },
    Node: { configurable: true, value: FakeNode },
    sessionStorage: {
      configurable: true,
      value: { getItem: () => null, removeItem() {} },
    },
    window: {
      configurable: true,
      value: {
        addEventListener() {},
        localStorage: {},
        navigator: { standalone: false },
        sessionStorage: {},
      },
    },
  });

  const appScript = new URL("../../app/static/js/app.js", import.meta.url);
  await import(`${appScript.href}?dropdown-test=${Date.now()}`);

  const clickListener = listeners.get("click");
  assert.equal(typeof clickListener, "function");

  clickListener({ target: clickedTarget });
  assert.equal(insideDropdown.closeCount, 0);
  assert.equal(otherDropdown.closeCount, 1);

  clickListener({ target: outsideTarget });
  assert.equal(insideDropdown.closeCount, 1);
  assert.equal(otherDropdown.closeCount, 2);
});

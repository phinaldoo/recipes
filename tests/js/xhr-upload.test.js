import assert from "node:assert/strict";
import test from "node:test";

import { UploadRequestError, uploadFormData } from "../../app/static/js/lib/xhr-upload.js";

class FakeTarget {
  listeners = new Map();

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || [];
    listeners.push(listener);
    this.listeners.set(name, listeners);
  }

  dispatch(name, event = {}) {
    for (const listener of this.listeners.get(name) || []) listener(event);
  }
}

class FakeXHR extends FakeTarget {
  upload = new FakeTarget();
  headers = new Map();
  responseHeaders = new Map([["content-type", "application/json"]]);
  responseText = "";
  status = 0;

  open(method, path) {
    this.method = method;
    this.path = path;
  }

  setRequestHeader(name, value) {
    this.headers.set(name, value);
  }

  getResponseHeader(name) {
    return this.responseHeaders.get(name.toLowerCase()) || null;
  }

  send(body) {
    this.body = body;
  }

  abort() {
    this.dispatch("abort");
  }
}

test("uploadFormData reports byte progress and resolves JSON responses", async () => {
  const xhr = new FakeXHR();
  const progress = [];
  let uploadComplete = false;
  const body = { backup: true };
  const resultPromise = uploadFormData("/restore", body, {
    csrfToken: "csrf-token",
    xhrFactory: () => xhr,
    onUploadProgress: (state) => progress.push(state),
    onUploadComplete: () => { uploadComplete = true; },
  });

  assert.equal(xhr.method, "POST");
  assert.equal(xhr.path, "/restore");
  assert.equal(xhr.body, body);
  assert.equal(xhr.withCredentials, true);
  assert.equal(xhr.headers.get("Accept"), "application/json");
  assert.equal(xhr.headers.get("X-CSRF-Token"), "csrf-token");

  xhr.upload.dispatch("progress", { lengthComputable: true, loaded: 50, total: 200 });
  xhr.upload.dispatch("load");
  xhr.status = 201;
  xhr.responseText = JSON.stringify({ valid: true });
  xhr.dispatch("load");

  assert.deepEqual(progress, [{ loaded: 50, total: 200, percent: 25 }]);
  assert.equal(uploadComplete, true);
  assert.deepEqual(await resultPromise, { valid: true });
});

test("uploadFormData exposes API validation errors", async () => {
  const xhr = new FakeXHR();
  const resultPromise = uploadFormData("/restore", {}, {
    fallbackMessage: "fallback",
    xhrFactory: () => xhr,
  });
  xhr.status = 422;
  xhr.responseText = JSON.stringify({ detail: "Invalid backup" });
  xhr.dispatch("load");

  await assert.rejects(resultPromise, (error) => {
    assert.ok(error instanceof UploadRequestError);
    assert.equal(error.status, 422);
    assert.equal(error.message, "Invalid backup");
    return true;
  });
});

test("uploadFormData can be cancelled with an AbortSignal", async () => {
  const xhr = new FakeXHR();
  const controller = new AbortController();
  const resultPromise = uploadFormData("/restore", {}, {
    signal: controller.signal,
    cancelledMessage: "Cancelled safely",
    xhrFactory: () => xhr,
  });
  controller.abort();

  await assert.rejects(resultPromise, (error) => {
    assert.equal(error.code, "cancelled");
    assert.equal(error.message, "Cancelled safely");
    return true;
  });
});

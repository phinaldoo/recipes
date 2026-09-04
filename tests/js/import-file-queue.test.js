import assert from "node:assert/strict";
import test from "node:test";

import { ImportFileQueue } from "../../app/static/js/lib/import-file-queue.js";

test("repeated mobile picker selections are appended", () => {
  const queue = new ImportFileQueue();
  const firstPhoto = { name: "photo.jpg", size: 100 };
  const secondPhoto = { name: "photo.jpg", size: 200 };

  queue.add([firstPhoto]);
  queue.add([secondPhoto]);

  assert.deepEqual(queue.files, [firstPhoto, secondPhoto]);
  assert.equal(queue.size, 2);
});

test("the queue keeps existing files when its limit is reached", () => {
  const queue = new ImportFileQueue(2);
  const first = { name: "first.jpg" };
  const second = { name: "second.jpg" };
  const rejected = { name: "third.jpg" };

  queue.add([first]);
  const result = queue.add([second, rejected]);

  assert.deepEqual(queue.files, [first, second]);
  assert.deepEqual(result.added, [second]);
  assert.deepEqual(result.rejected, [rejected]);
});

test("files can be removed without exposing mutable queue state", () => {
  const queue = new ImportFileQueue();
  const first = { name: "first.jpg" };
  const second = { name: "second.pdf" };
  queue.add([first, second]);

  const snapshot = queue.files;
  snapshot.length = 0;
  assert.equal(queue.size, 2);

  assert.equal(queue.remove(0), first);
  assert.deepEqual(queue.files, [second]);
});

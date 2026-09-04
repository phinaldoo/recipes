import assert from "node:assert/strict";
import test from "node:test";

import {
  isSupportedRecipeImage,
  RecipeImageQueue,
  transferredFiles,
} from "../../app/static/js/lib/recipe-image-queue.js";

test("recipe images accept every server-supported format", () => {
  assert.equal(isSupportedRecipeImage({ name: "gericht.jpg", type: "image/jpeg" }), true);
  assert.equal(isSupportedRecipeImage({ name: "gericht.png", type: "image/png" }), true);
  assert.equal(isSupportedRecipeImage({ name: "gericht.webp", type: "image/webp" }), true);
  assert.equal(isSupportedRecipeImage({ name: "gericht.gif", type: "image/gif" }), true);
  assert.equal(isSupportedRecipeImage({ name: "aufnahme.HEIC", type: "" }), true);
  assert.equal(isSupportedRecipeImage({ name: "rezept.pdf", type: "application/pdf" }), false);
  assert.equal(isSupportedRecipeImage({ name: "scan.tiff", type: "image/tiff" }), false);
});

test("clipboard and drop transfers expose their file items", () => {
  const pastedImage = { name: "image.png", type: "image/png" };
  const fallbackImage = { name: "fallback.jpg", type: "image/jpeg" };

  assert.deepEqual(
    transferredFiles({
      items: [
        { kind: "string", getAsFile: () => null },
        { kind: "file", getAsFile: () => pastedImage },
      ],
      files: [fallbackImage],
    }),
    [pastedImage],
  );
  assert.deepEqual(
    transferredFiles({ items: [{ kind: "file", getAsFile: () => null }], files: [fallbackImage] }),
    [fallbackImage],
  );
});

test("pasted, dropped, and selected images share one removable queue", () => {
  const queue = new RecipeImageQueue();
  const pastedPng = { name: "image.png", type: "image/png", size: 100 };
  const selectedJpeg = { name: "kuchen.jpg", type: "image/jpeg", size: 200 };
  const droppedPdf = { name: "rezept.pdf", type: "application/pdf", size: 300 };

  const pasted = queue.add([pastedPng], "clipboard");
  const selected = queue.add([selectedJpeg], "picker");
  const rejected = queue.add([droppedPdf], "drop");

  assert.equal(pasted.added[0].name, "eingefuegtes-bild-1.png");
  assert.equal(pasted.added[0].source, "clipboard");
  assert.equal(selected.added[0].name, "kuchen.jpg");
  assert.deepEqual(rejected.rejected, [droppedPdf]);
  assert.equal(queue.size, 2);

  const snapshot = queue.entries;
  snapshot.length = 0;
  assert.equal(queue.size, 2);
  assert.equal(queue.remove(0)?.name, "eingefuegtes-bild-1.png");
  assert.deepEqual(queue.entries.map((entry) => entry.name), ["kuchen.jpg"]);
});

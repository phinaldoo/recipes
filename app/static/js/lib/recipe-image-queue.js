const SUPPORTED_IMAGE_TYPES = new Set([
  "image/gif",
  "image/heic",
  "image/heif",
  "image/jpeg",
  "image/png",
  "image/webp",
]);

const SUPPORTED_IMAGE_EXTENSION = /\.(?:gif|heic|heif|jpe?g|png|webp)$/i;
const TYPE_EXTENSIONS = {
  "image/gif": "gif",
  "image/heic": "heic",
  "image/heif": "heic",
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/webp": "webp",
};

function imageExtension(file) {
  const filename = typeof file?.name === "string" ? file.name : "";
  const filenameExtension = filename.match(SUPPORTED_IMAGE_EXTENSION)?.[0].slice(1);
  if (filenameExtension) return filenameExtension.toLocaleLowerCase("de");
  return TYPE_EXTENSIONS[String(file?.type || "").toLocaleLowerCase("en-US")] || "bild";
}

export function isSupportedRecipeImage(file) {
  if (!file) return false;
  const mimeType = String(file.type || "").toLocaleLowerCase("en-US");
  const filename = typeof file.name === "string" ? file.name : "";
  return SUPPORTED_IMAGE_TYPES.has(mimeType) || SUPPORTED_IMAGE_EXTENSION.test(filename);
}

export function transferredFiles(dataTransfer) {
  if (!dataTransfer) return [];
  const itemFiles = Array.from(dataTransfer.items || [])
    .filter((item) => item.kind === "file")
    .map((item) => item.getAsFile?.())
    .filter(Boolean);
  return itemFiles.length > 0 ? itemFiles : Array.from(dataTransfer.files || []);
}

export class RecipeImageQueue {
  constructor() {
    this._entries = [];
    this._clipboardImageCount = 0;
  }

  get entries() {
    return this._entries.slice();
  }

  get size() {
    return this._entries.length;
  }

  add(fileList, source = "picker") {
    const added = [];
    const rejected = [];
    Array.from(fileList || []).forEach((file) => {
      if (!isSupportedRecipeImage(file)) {
        rejected.push(file);
        return;
      }
      let name = String(file.name || "").trim();
      if (source === "clipboard") {
        this._clipboardImageCount += 1;
        name = `eingefuegtes-bild-${this._clipboardImageCount}.${imageExtension(file)}`;
      } else if (!name) {
        name = `bild-${this._entries.length + 1}.${imageExtension(file)}`;
      }
      const entry = { file, name, source };
      this._entries.push(entry);
      added.push(entry);
    });
    return { added, rejected };
  }

  remove(index) {
    if (!Number.isInteger(index) || index < 0 || index >= this._entries.length) return null;
    return this._entries.splice(index, 1)[0];
  }
}

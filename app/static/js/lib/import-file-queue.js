export class ImportFileQueue {
  constructor(limit = 20) {
    if (!Number.isInteger(limit) || limit < 1) {
      throw new RangeError("The import file limit must be a positive integer.");
    }
    this.limit = limit;
    this._files = [];
  }

  get files() {
    return this._files.slice();
  }

  get size() {
    return this._files.length;
  }

  add(fileList) {
    const incoming = Array.from(fileList || []);
    const available = Math.max(0, this.limit - this._files.length);
    const added = incoming.slice(0, available);
    const rejected = incoming.slice(available);
    this._files.push(...added);
    return { added, rejected };
  }

  remove(index) {
    if (!Number.isInteger(index) || index < 0 || index >= this._files.length) return null;
    return this._files.splice(index, 1)[0];
  }
}

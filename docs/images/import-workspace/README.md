# Import workspace visual proof

Screenshots of the real FastAPI application rendered in Chromium at 1440 px and 390 px widths. The app uses seeded demonstration imports and food photos; no production data is shown.

- `workspace-*`: the combined composer, searchable history, and status filters.
- `upload-*`: photos/PDF upload and the recipe-package option.
- `review-*`: slide-over selection with a same-title warning; matching recipes start unchecked.
- `editor-*`: ingredient and instruction editing in the review panel.
- `saved-desktop.png`: the result after saving two selected recipes through the real confirmation API.

The browser check exercised English, German, Chinese, Hindi, and Spanish at 320, 390, 768, 1024, and 1440 px. It verified no horizontal overflow, visible save controls, edits surviving panel close/reopen, and a corrected ingredient amount appearing in the saved recipe.

Extraction results were seeded to make these screenshots repeatable. The screenshots do not claim a live external-site extraction or AI run. Upload submission/failure handling, JSON package import, authorization, and atomic candidate promotion have separate automated coverage.

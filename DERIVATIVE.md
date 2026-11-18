# Derivative notes

Changes on top of Cairn `231768005259937f3425280b2f1ae3f6ecb959e3`:

- `src/cairn/family.py` turns on the Jojo grade-4 profile and blocks tutor, recall, and cloud vision calls.
- `src/cairn/models/material.py` plus migration v33 store file and page provenance.
- `src/cairn/material_check.py` and `src/cairn/material_ingest.py` render local PDFs, run local OCR, classify pages, deduplicate companion files, and publish only checked items.
- `src/cairn/material_web.py` and templates `learn.html` / `materials.html` add the learner home and the parent review list. Studio stays a separate app.
- `src/cairn/web.py`, `tutor/llm.py`, `digitize/client.py`, and `templates/settings.html` call those hooks. Unset `CAIRN_FAMILY_PROFILE` and the stock Cairn paths stay as they were.
- `pypdf`, `pypdfium2`, and `pillow` are declared so PDF pages can be rendered locally.

No plugin marketplace, branches, or loops were added. Model tutoring and text-recall cloud calls stay off for the family profile.

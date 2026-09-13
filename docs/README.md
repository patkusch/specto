# Pictures for the README
- `report-*.png` are three parts of `report.html` (a requirement card with its acceptance criteria, the screen-flow diagram, the SME questions table); `workbook-*.png` are the first rows of the Requirements and SME Questions sheets of `analysis.xlsx` drawn as a table; `frame-example.png` is one frame from the example recording.
- They were made from a `--fake` run of `examples/onboarding`, so the words in them are the stand-in model's placeholders; only the frame is real.
- To regenerate: run specto into some folder, then `.venv/bin/python docs/make_screenshots.py OUT_DIR`.
- The script needs Playwright with chromium, openpyxl and Pillow in the venv, and keeps every PNG under 400 KB.
- Regenerate them after the first run with a real model, so the README stops showing placeholder text.

Regenerated 2026-09-13 from a real reading of the example (see examples/onboarding/reference/).

# Changelog

Newest first. Dates are when the change was pushed.

## 0.2.0 (2026-09-12)

- Live mode: `specto live` watches the shared screen and microphone during
  a call and refreshes a questions file every five minutes. Each round reuses
  what the model already read, so only new frames are paid for.
- One-file HTML report with the frames embedded and answer cells for the
  follow-up call.
- Jira and Azure DevOps import files written next to the workbook.
- Cost estimate printed before every real run; `--estimate` compares models;
  `specto doctor` lists what is installed and missing.
- Speaker names shown to the model when the transcript has them.
- Every requirement and criterion checked against plain writing rules, with
  a Writing check column in the workbook.
- Each still compared with the one before it; small changes get a close-up
  crop so typed values and pressed buttons can be read.
- Word-level timing from local speech-to-text, with sentences cut at the word
  where the screen changed; stable-ts as an optional extra for closer timing.

## 0.1.0 (2026-09-10)

- First release: still frames at each screen change, transcript from file
  or local speech-to-text, Claude reads frames and words in batches and one
  merge pass, workbook with nine sheets and a Markdown report.
- Screen changes found by image fingerprint rather than brightness, so
  colour-only changes and typed text count.
- Optional OCR of each frame, shown to the model next to the image.
- `specto score` against a hand-written answer key; a narrated example
  recording of a fake onboarding tool with its answer key.

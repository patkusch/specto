# Changelog

Newest first. Dates are when the change was pushed.

## 0.6.0 (2026-09-14)

- The example's sample customer is generated from a seed with values that
  cannot be real, instead of being invented by the model.
- A second example: a complaints tool recorded inside a meeting frame, with
  its own answer key. `--crop auto` finds the shared window to the pixel.
- Reference readings for both examples, produced by Claude through the
  bring-your-own-model path: recall 0.93 and 0.97 on two readings of the
  onboarding example, 1.00 on the complaints example.
- An hour-long stress run for ingest (about 20 seconds, under 650 MB at
  full size), which found and fixed stale close-ups surviving a re-run.
- README reordered: short opening, results first, reference material last.

## 0.5.0 (2026-09-13)

- Live mode runs on Windows and Linux as well as macOS, and on a Mac
  without Screen Recording permission it stops with a clear line instead of
  recording an empty desktop.
- `--crop auto` finds the shared window inside a meeting recording and
  drops the border, toolbar and gallery strip; `--crop x,y,w,h` sets it by
  hand. Over the frame cap (now 240), the least-changed frames go first.
- File transcripts are cut at the screen change too, with word times
  estimated from word length.
- `--max-cost` stops before the model when the estimate is above a cap.
- Every response shape is checked against the SDK's structured-output
  schema rules in the test suite.

## 0.4.0 (2026-09-13)

- A model run that dies halfway resumes from the last finished chunk; a
  chunk cut off at the output limit is read again as two halves;
  `--reader-model` uses a cheaper model for reading the frames.
- Questions ranked by how many requirements each one holds up, in the
  workbook, the reports and the live questions file.
- Gaps sheet: screens with no requirement, fields never mentioned,
  requirements with no criteria, buttons that lead nowhere, and more.
- `specto redact` paints over personal data on the frames and masks it in
  the outputs; `specto restore` undoes it.
- `specto run` accepts a folder of screenshots and written notes when there
  is no recording.
- Pictures of the output at the top of the README.

## 0.3.0 (2026-09-12)

- Glossary sheet built from the analysis, with a naming check for
  near-duplicate terms and roles missing from the actors list.
- Personal Data sheet: every email, phone number, postcode, date of birth,
  card or account number, name and address the recording captured, masked,
  with the frame it was on.
- Screen-flow picture in the HTML and Markdown reports.
- `specto answers` reads the Answer and Status columns typed into the
  workbook back in; `specto resolve` turns answered questions into
  requirements, criteria and follow-ups.
- `specto merge` folds several sessions into one workbook without a model
  call.
- Changelog and a commands table in the README.

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

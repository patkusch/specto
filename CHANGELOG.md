# Changelog

Newest first. Dates are when the change was pushed.

## 0.8.0 (2026-09-22)

**Who said what.** `--speakers` labels each transcript segment that does not
already carry a speaker name with a speaker cluster found from the
recording's own audio, entirely on your machine.

- A previous look at this (`PLAN.md`'s Phase 4 note) found only pyannote's
  own diarization pipeline, which needs a Hugging Face account and a gated
  model download. That is no longer the only option: sherpa-onnx runs the
  same segmentation model (pyannote's `segmentation-3.0`, re-exported to
  ONNX) plus a speaker-embedding model, both downloaded straight from a
  public GitHub release with no account, no token, and nothing to accept.
  New optional extra, `pip install "specto[speakers]"`.
- `specto/diarize.py`: `assign_speakers` extracts audio with the bundled
  ffmpeg, runs sherpa-onnx's offline diarization, and fills in `.speaker`
  by whichever diarized turn overlaps a segment most; a segment already
  named by a `<v Name>` VTT tag or a meeting export is left alone.
- `--speakers` on `specto run`; off by default, since it is a new
  dependency. `specto doctor` reports whether it is installed and whether
  the two models are downloaded yet.
- Measured, not assumed: a three-voice test clip was built for this feature
  (three macOS `say` voices, six turns, `tests/fixtures/diarize/`) with a
  written-down ground truth. Diarization got all 6 turns onto the right
  speaker cluster and found all 5 true speaker changes within 0.1s, with no
  false ones. That is one clip with three clearly different voices and
  clean gaps between turns; it says nothing yet about two similar-sounding
  people, cross-talk, or a noisy room. `tests/test_diarize.py` runs this
  check for real when the extra is installed (skipped otherwise, so the
  default test matrix does not need the new dependency).
- 701 offline tests (was 688).

## 0.7.0 (2026-09-20)

**Scores.** The recall figures are now labelled by how fair they are. The
onboarding 1.00 is a training score: the reading prompt was changed on
2026-09-16 after an earlier reading scored 0.93, so the figure shows the
prompt fits that recording. The complaints 1.00 was a regression check
(1.00 before and after the same change). The new held-out example, a phone
app whose answer key was written before any reading and which no prompt was
tuned against, scored **0.94** on a blind reading. That is one recording, a
small sample. The README, the scoreboard and its pictures say this, and the
0.6.0 figures below should be read the same way.

- A third example, `examples/deliveries/`: a portrait phone-app recording
  with no meeting frame, an answer key written first, and a blind reference
  reading (screens 6 of 6, fields 12 of 13, actions 4 of 4, requirements
  7 of 7, questions 4 of 5; both screen-only questions found).
- The reading prompt asks for one requirement per independently mandatory
  item, and for a follow-up question when the expert names only a few
  examples of an accepted value. Both onboarding and complaints readings
  were redone under it. Judge it by the held-out score, not by the
  onboarding one.
- `specto doctor --ping` makes one tiny real call through the same code a
  run uses and says what is wrong in one plain sentence (missing or invalid
  key, no access to the model, rejected request, rate limit or no credit,
  Gemini API not switched on, network), or prints the request id, tokens,
  cost in cents, time and `ready`. Nothing is sent without a key, and no
  part of a key is ever printed.
- `--crop auto` no longer trims a full-frame app recording. It crops only
  when two opposite sides have a static margin of at least 5% of the frame,
  as a shared window inside a meeting frame does. Phone recordings kept
  losing their status bar and left edge before; the claims example still
  crops to the pixel. OCR reads best at 720 px wide or more, so record
  phone screens at full resolution.
- `specto demo` runs a real example end to end with no key and no model
  call, and there is a Dockerfile with a CI job that builds it and runs
  `doctor` and the demo.
- `specto watch` processes every recording or screenshot folder dropped
  into a shared folder, skips what is done, redoes what changed, and keeps
  going past a bad one.
- Confluence and SharePoint pages join the Jira and Azure DevOps import
  files.
- `specto compare` reports what was added, dropped or reworded between two
  finished readings of a journey.
- Answers typed into `report.html` (with a status) survive closing the
  page, export as a file, and read back into the workbook with
  `specto answers --from-html`.
- A `specto.toml` project settings file (`specto init` writes a commented
  starter): a flag beats the file, the file beats the default, and anything
  that looks like a key is refused. `doctor` shows what it found.
- `doctor` reports whether a Gemini key is set (never its value) and which
  model service a run can use.
- The README opens on a dark dashboard demo built from the real reading,
  with the scoreboard page beside it, and both are regenerated from the
  reference score files.
- 688 offline tests.

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

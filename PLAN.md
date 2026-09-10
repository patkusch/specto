# Plan

## What this is

A subject-matter expert records themselves walking through a system on a shared
screen: "this is the customer screen, here we type the postcode, then we press
Save and it goes to the approvals queue". Today someone has to sit through that
recording and type up what the system must do.

specto watches the recording instead. It listens to what was said, looks at what
was on screen at that moment, and writes a spreadsheet a business analyst can
hand straight to a delivery team:

- the journey, screen by screen
- every data field seen or mentioned, with where it appeared
- user requirements, each pointing at the moment in the recording it came from
- acceptance criteria for each requirement
- questions to take back to the expert, with the exact timestamp that raised them

Every row links to a still image from the recording, so a reader can check the
claim against the screen in one click.

## Stages

| Stage | Input | Output | Done by |
|---|---|---|---|
| 1. Ingest | video file (+ optional transcript file) | `recording.json`: timed transcript, still frames at each screen change, and "moments" (a frame plus what was said while it was showing) | `specto/ingest.py`, `specto/transcript.py`, `specto/align.py` |
| 2. Extract | `recording.json` + frame images | `analysis.json`: screens, fields, actions, journey, requirements, acceptance criteria, questions | `specto/extract.py` (Claude, vision + text) |
| 3. Export | `analysis.json` | `analysis.xlsx`, `report.md` | `specto/export.py` |

Each stage writes its result to the output folder and the next stage reads it, so
a re-run after a crash or a prompt tweak skips the finished stages.

## MVP scope (this pass)

- Recorded files only (mp4, mov, mkv, webm; anything ffmpeg reads).
- Transcript: bring your own (.vtt, .srt, .txt with timestamps, or the JSON
  Zoom/Teams/Loom export) **or** local speech-to-text via faster-whisper when
  it is installed. Most meeting tools already produce a transcript, so this is
  the common path.
- Still frames: ffmpeg scene-change detection, then near-duplicate removal with
  a perceptual hash, capped at a configurable maximum so a long recording stays
  affordable.
- Extraction: Claude Opus 5 with structured JSON output. Two passes: per-chunk
  reading of frames + transcript, then one consolidation pass over the whole
  session. The model caller is injectable so tests run with no network.
- Export: one workbook, one Markdown report, frames folder, all cross-linked.
- Tests in CI with no API key and no network: a synthetic recording is built
  with Pillow + ffmpeg inside the test.

## Roadmap

Three things need a person: an API key in the shell, mic and screen permission
on the Mac for live mode, and real recordings from real experts. Everything
else can be built and checked without them.

### Phase 1: prove it on realistic input, no key needed
- A realistic example recording in `examples/onboarding/`: a small fake
  onboarding web app rendered in a browser, screenshotted step by step, encoded
  to video, narrated with the Mac's text-to-speech so it has real audio, plus
  the script as a `.vtt`.
- Local speech-to-text checked end to end on that audio.
- Screen-change detection by image hash (catches colour-only changes) with the
  ffmpeg brightness detector kept as an option.
- Optional text reading (OCR) of each frame, fed to the model with the image,
  and a no-model "text seen on screen" fallback.
- `specto score`: compare an output with a hand-written expected result, so
  prompt changes can be measured.

### Phase 2: first real run (needs the key)
Run the example through Claude Opus 5, record the cost, tune the prompts
against the score, save the output as the reference result. Compare Sonnet 5.

### Phase 3: live mode
`specto live`: screenshot every few seconds, mic to text in rolling windows,
the questions list growing during the call. Built with a replay mode first.

### Phase 4: where the output goes
Jira and Azure DevOps import files, speaker labels, a review page where the
analyst edits and ticks rows before export.

### Phase 5: hardening
Three to five real recordings, a cost line per run, one-command install.

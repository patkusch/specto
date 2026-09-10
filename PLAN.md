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

## Later (not in this pass)

- **Live mode**: a `specto live` command that grabs the shared screen every
  few seconds (macOS `screencapture`) and records the microphone, then feeds
  the same pipeline in rolling windows so the questions list grows during the
  call and can be asked before the expert leaves.
- Speaker labels (who said what) when the transcript carries them.
- OCR fallback for text on screen when the frame is too small to read.
- Jira / Azure DevOps export of the requirements and criteria.

## Agent split for the build

Three agents build the three stages in parallel against the shared data model
in `specto/model.py`. The integrator wires the CLI, runs the end-to-end test,
writes the README and pushes to GitHub.

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

### Phase 3: live mode (built 2026-09-12, not yet run on a real call)
`specto live`: screenshot every few seconds, mic to text in rolling windows,
the questions list growing during the call. Built with a replay mode first;
the replay path is tested, the capture path needs a Mac with Screen Recording
and Microphone permission granted to the terminal. Each analysis round
reuses the chunk readings from earlier rounds and only sends what is new.

### Phase 4: where the output goes
Jira and Azure DevOps import files (done 2026-09-11), speaker names in what
the model reads (done), a single-file HTML report with answer cells (done
2026-09-12). Still open: speaker labels from audio, and a review step where
the analyst's edits flow back into the workbook.

### Phase 5: hardening
Three to five real recordings, a cost line per run, one-command install.

## What we reuse from elsewhere, and what we checked and kept our own

Looked at on 2026-09-10 (stars and dates from GitHub that day).

| Piece | Candidate | Decision |
|---|---|---|
| Screen-change detection | PySceneDetect (5.2k stars, BSD) | **Kept our own.** Its `HashDetector` turns every frame grey before hashing, and its `ContentDetector` averages over the whole frame, so both miss the two cases our detector is tested on: a colour-only change and a few typed values on a white form. |
| Near-duplicate frames | imagehash (3.9k, BSD) | Not needed; our fingerprint already does this. |
| Speech-to-text | faster-whisper (in use); stable-ts (2.3k, MIT) for word-level timestamps | **Later.** Word times would let a sentence that spans two screens be split at the right word. Today sentences are placed by their midpoint, which is fine for the example. |
| Speaker labels | whisperX (24k, BSD) + pyannote (10.5k, MIT code, gated model) | **Phase 4.** Needs a Hugging Face account for the diarization model. |
| Transcript files | webvtt-py, srt | Not adopted; our parser handles .vtt, .srt, timestamped .txt and meeting-tool .json with ten tests. Zoom, Teams and Loom all export .vtt. |
| OCR | rapidocr-onnxruntime | **Adopted** (pip only, models bundled, runs on CPU). |
| Live capture | screenpipe (21.5k stars; now a commercial licence, free only for personal use), rem (unmaintained), openrecall (AGPL) | **Design reference only.** screenpipe's local search API (frames + OCR + transcript by time window) is the shape `specto live` should expose. Licence rules it out as a dependency for work use. |
| Step-by-step guides from a browser session | Mimik (773 stars, MIT), OpenAdapt (1.7k, MIT) | **Idea only.** Both record live browser or desktop sessions with click positions; neither reads a video file or audio. Their per-step output shape is a good model for the Journey sheet. |
| Requirements from transcripts | a handful of small prompt demos | Nothing to adopt. |
| Jira / Azure DevOps | both import CSV natively; pycontribs/jira and azure-devops-python-api for API push | **Phase 4:** write their CSV import format first, no library needed. |

Searched and found nothing usable: an open-source tool that goes from a
video with narration to requirements; a Scribe or Tango equivalent that works
on a recorded file; a maintained parser for Zoom, Teams or Loom JSON exports;
a screen-change detector tuned for mostly-static screen shares.

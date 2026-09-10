# Onboarding example

A stand-in for a real screen share: an expert talks through a fake internal tool called Northwind Onboarding, six screens from Customer Search to Confirmation, with real spoken audio. Nothing in it is a real person or company.

`walkthrough.mp4` (about 1.2 MB, 112 seconds) is the recording, `walkthrough.vtt` is the transcript with the same timing, and `expected.json` is the hand-written answer key: the screens, fields, actions, requirements and open questions a good run should find.

How it was made: `app/` holds the six pages as plain HTML and CSS. `make_example.py` opens each page in a headless browser (Playwright) and saves a 1280x720 picture, speaks each narration line with the Mac's `say` command, measures each clip with ffmpeg, then joins the audio, builds the video so each screen stays up while its lines are spoken, and writes the VTT from the same timeline. If Playwright is missing it draws the screens with Pillow instead.

To rebuild: `.venv/bin/pip install playwright && .venv/bin/playwright install chromium`, then `.venv/bin/python examples/onboarding/make_example.py` from the repo root. It prints the timeline and overwrites the mp4 and vtt.

To run specto on it without an API key: `specto run examples/onboarding/walkthrough.mp4 --transcript examples/onboarding/walkthrough.vtt --fake`.

Two things measured on 2026-09-10. Local speech-to-text (faster-whisper, base model) on the audio got 14 words wrong out of 335, a word error rate of about 4%; the mistakes were small ("verify it" for "verified"). The ffmpeg scene detector found none of the five screen changes, because all six pages have the same light background, so ingest fell back to one frame every 10 seconds (11 frames); a hash-based detector is planned to fix that.

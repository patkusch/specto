# Onboarding example

A stand-in for a real screen share: an expert talks through a fake internal tool called Northwind Onboarding, six screens from Customer Search to Confirmation, with real spoken audio. Nothing in it is a real person or company.

`walkthrough.mp4` (about 1.2 MB, 112 seconds) is the recording, `walkthrough.vtt` is the transcript with the same timing, and `expected.json` is the hand-written answer key: the screens, fields, actions, requirements and open questions a good run should find.

How it was made: `templates/` holds the six pages with `{{placeholders}}` where a name, date, number or street goes, and `make_example.py` fills them in and writes the finished pages to `app/`. Every one of those values comes from `sample_customer()` in `make_example.py`, built from a fixed seed so the example never shows a real person: the names are glued together from syllables, the date of birth is always 1 January, the phone number is in the 07700 900xxx range Ofcom keeps for fiction, the email is at example.com and the street is called something like Example Street (the postcode stays SW1A 1AA because the narration says it). To get a different but equally fictional customer, change `SEED` at the top of `make_example.py` and rebuild. The script then opens each page in a headless browser (Playwright) and saves a 1280x720 picture, speaks each narration line with the Mac's `say` command, measures each clip with ffmpeg, then joins the audio, builds the video so each screen stays up while its lines are spoken, and writes the VTT from the same timeline. If Playwright is missing it draws the screens with Pillow instead.

To rebuild: `.venv/bin/pip install playwright && .venv/bin/playwright install chromium`, then `.venv/bin/python examples/onboarding/make_example.py` from the repo root. It prints the timeline and overwrites the mp4 and vtt.

To run specto on it without an API key: `specto run examples/onboarding/walkthrough.mp4 --transcript examples/onboarding/walkthrough.vtt --fake`.

Two things measured on 2026-09-10. Local speech-to-text (faster-whisper, base model) on the audio got 14 words wrong out of 335, a word error rate of about 4%; the mistakes were small ("verify it" for "verified"). The ffmpeg scene detector found none of the five screen changes, because all six pages have the same light background, so ingest fell back to one frame every 10 seconds (11 frames); a hash-based detector is planned to fix that.

## Reference result

`reference/` holds a real reading of this recording: the answers a Claude
model gave to the two request files (`chunk_01.response.json`,
`consolidate.response.json`), the analysis built from them, its score
against `expected.json` (overall recall 1.00) and the Markdown report. This is the example the reading prompt was tuned against: an earlier reading scored 0.93, the prompt was changed on 2026-09-16, and this reading came after. Treat the 1.00 as a training score, not evidence; the held-out score is in `examples/deliveries/`. Use
it to compare a new prompt or a new model: run the pipeline, then diff the
score.

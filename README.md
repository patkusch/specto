# specto

Watches a screen recording of an SME [expert] walking through a system and writes
down what the system must do.

An expert records themselves clicking through the screens and talking: "this
is where we look up the customer, we type the postcode here, then Save sends it
to the approvals queue". Today a business analyst sits through the recording and
types that up by hand. specto does the first draft. It listens to what was said,
looks at what was on screen at that moment, and produces one spreadsheet:

| Sheet | What is in it |
|---|---|
| Journey | The process step by step, one row per screen visited |
| Screens | Every screen seen, what it is for, and a picture of it |
| Data Fields | Every box, dropdown and column seen or mentioned, with the values on screen |
| Actions | Every button and link the expert used, and where it led |
| Requirements | What the system must do, in the expert's own words, with a picture of the moment |
| Acceptance Criteria | How to check each requirement is met (Given / When / Then) |
| SME Questions | What the expert did not say and someone must ask before building |
| Transcript | Everything said, with the time and the screen that was showing |

Every row links to a still image from the recording, so a reader can check any
claim against the screen in one click. A Markdown report with the same content
is written next to the workbook.

## Try it

```bash
pip install -e .
export ANTHROPIC_API_KEY=...
specto run walkthrough.mp4 --transcript walkthrough.vtt
```

Output lands in `out/walkthrough/`: `analysis.xlsx`, `report.md`,
`report.html`, a `frames/` folder, two ticket import files, and the JSON
files the stages hand to each other.

**One file to send.** `report.html` is the whole analysis in a single page
with the frames inside it, so it can be emailed or dropped in a chat and
opens anywhere with nothing else attached. The SME Questions table has
answer cells you can type into during the follow-up call, then print to PDF.

**Into Jira or Azure DevOps.** `jira_import.csv` holds one Story per
requirement with its acceptance criteria and source in the description, and
one Task per question. In Jira go to Settings, System, External System
Import, CSV, and pick the file; the columns map by name. `azure_devops_import.csv`
holds one User Story per requirement and one Issue per question. In Azure
DevOps go to Boards, Queries, Import Work Items, pick the file, then Save
items. On a Scrum or Basic project change "User Story" to "Product Backlog
Item" in the file first.

If you have no transcript file, leave `--transcript` off and specto transcribes
the audio on your machine (needs `pip install -e ".[whisper]"`; the first run
downloads a speech model). Every word then gets its own start and end time, so
a sentence that runs across a screen change is cut at the word and each screen
only gets what was said while it was showing. Word times land within about a
third of a second; `pip install -e ".[whisper-precise]"` adds stable-ts, which
lands them about three times closer at the price of a 600 MB download.

Transcript files from meeting tools (`.vtt`, `.srt`) carry no word times, so
those sentences go with the screen showing at their midpoint. They are still
the faster and usually more accurate route when you have them.

To check the pipeline without spending anything, `--fake` runs it with a
stand-in model. There is a ready-made example recording in the repo, a
two-minute narrated walkthrough of a fake onboarding tool:

```bash
specto run examples/onboarding/walkthrough.mp4 --transcript examples/onboarding/walkthrough.vtt --fake
```

With `pip install -e ".[ocr]"` the text on each frame is also read and given
to the model next to the image, so small labels and values are not lost when
the frame is scaled down. It is on by default when installed; `--no-ocr` turns
it off.

## During a call, not after it

```bash
specto live --out out/todays-call
```

On a Mac this grabs the shared screen every three seconds and records the
microphone in thirty-second pieces, keeps only the moments the screen
changed, and every five minutes runs the whole session so far through the
same analysis. `out/todays-call/live_questions.md` holds just the open
questions, newest first, so it can sit in a window and be asked before the
expert leaves. Press Ctrl-C to stop; the full workbook and reports are
written at the end. macOS will ask once for Screen Recording and Microphone
permission for your terminal. If the folder already exists the session is
resumed. To try it without a call, `--replay DIR --transcript FILE` feeds a
folder of screenshots named `shot_<seconds>.png` through the same path.

## How it works

1. **Ingest.** One frame per second is compared with the last kept frame
   using an image fingerprint that notices colour and typed text, and a still
   is saved whenever the screen changed. The transcript is read (or made),
   and each sentence is matched to the image that was showing when it was
   said. Each still is compared with the one before it to find where the
   screen changed; a small change (a typed value, a pressed button) gets a
   close-up crop so the model can read it. If OCR is installed, the text on
   each still is read too.
2. **Extract.** Claude reads the images and the words in batches of eight
   frames and writes down the screens, fields, actions and candidate
   requirements it sees, keeping the same screen names across batches. One
   final pass over the whole session merges those notes into requirements with
   acceptance criteria and a list of questions.
3. **Export.** The notes become the workbook and the report, with every row
   pointing at its frame. Every requirement and criterion is checked against
   plain writing rules (one thought per sentence, no vague words, no escape
   clauses, a visible outcome) and the findings go in a "Writing check"
   column, so a reader sees at a glance which rows need a rewrite.

Each stage saves its result in the output folder. Running the same command
again skips the stages already done, so a prompt change re-runs only the
extraction, and a workbook layout change re-runs only the export
(`specto export out/walkthrough`). `--force` redoes everything.

## Commands

| Command | What it does |
|---|---|
| `specto run VIDEO` | the whole pipeline on a recording |
| `specto live --out DIR` | screen and microphone during a call, questions every five minutes |
| `specto export DIR` | rebuild every output from a finished analysis |
| `specto score DIR KEY` | compare an output with an answer key |
| `specto doctor` | what is installed and what is missing |
| `specto merge DIR DIR... --out DIR` | several sessions into one workbook: same screens, requirements and questions folded together, no model call |

## Options

| Flag | Meaning | Default |
|---|---|---|
| `--transcript FILE` | `.vtt`, `.srt`, timestamped `.txt`, or a meeting-tool `.json` | transcribe locally |
| `--out DIR` | where to write | `out/<video name>` |
| `--model ID` | Claude model | `claude-opus-5` |
| `--effort` | how hard the model thinks: low, medium, high, xhigh, max | `high` |
| `--frames-per-call N` | images per model call | 8 |
| `--max-frames N` | cap on still images kept | 120 |
| `--detect hash|scene` | find screen changes by image fingerprint, or by ffmpeg brightness | `hash` |
| `--hash-distance N` | how different a frame must be to count as new; lower catches typed text | 8 |
| `--sample-fps X` | frames looked at per second in hash mode | 1 |
| `--scene-threshold X` | brightness change that counts as a new screen, scene mode only | 0.3 |
| `--ocr / --no-ocr` | read the text on each frame for the model | on |
| `--ingest-only` | stop after frames and transcript | |
| `--fake` | stand-in model, no key needed | |
| `--force` | redo every stage | |

## Checking the output against an answer key

`examples/onboarding/expected.json` lists what a good analysis of the example
must contain: screen names, field labels, and keyword groups for actions,
requirements and questions. After a run, the score command reports how much
of it was found and what was missed:

```bash
specto score out/walkthrough examples/onboarding/expected.json --min 0.7
```

This is how a prompt change is judged: run, score, compare. Write an answer
key for your own recording the same way.

## What it costs

Before any real run specto prints what it expects to spend, and
`--estimate` prints the figure for each model and stops:

```bash
specto run walkthrough.mp4 --transcript walkthrough.vtt --estimate
```

`specto doctor` lists what is installed and what is missing (ffmpeg, the
speech model, OCR, the API key) and what to do about each.

A one-hour walkthrough typically yields 60 to 120 still images. Each image is
scaled to 1280 pixels wide and costs roughly 1,500 tokens to read, and the
system prompt is cached between calls. Expect a few dollars per hour of
recording on Claude Opus 5; `--effort medium` and a lower `--max-frames` cut
that further.

## Limits

- Live mode is macOS only. Each analysis round reuses what the model
  already read, so only the newest frames and one merge call are paid for
  each time.
- It writes a first draft. The Requirements sheet carries a confidence column:
  "high" means the expert said it plainly, "low" means it was inferred from
  the screen. Read the low ones with care.
- Small text on a high-resolution screen may be unreadable at 1280 pixels
  wide; raise `max_width` in `specto/ingest.py` if fields are being missed.
- Speaker names appear only when the transcript file carries them.
- Screen changes are found by taking one still a second and keeping it when
  its layout, text or colour differs from the last one kept by more than
  `--hash-distance` (default 8; lower it to catch smaller changes, raise it
  to ignore a ticking clock or a moving cursor); `--detect scene` switches to
  ffmpeg's brightness detector, which misses a page that only changes colour
  or gains typed text.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The tests build a small synthetic video, run every stage with a stand-in model,
and open the resulting workbook. They need no network and no API key.

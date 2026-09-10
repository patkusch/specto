# specto

Watches a screen recording of an expert walking through a system and writes
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

Output lands in `out/walkthrough/`: `analysis.xlsx`, `report.md`, a `frames/`
folder, and the two JSON files the stages hand to each other.

If you have no transcript file, leave `--transcript` off and specto transcribes
the audio on your machine (needs `pip install -e ".[whisper]"`; the first run
downloads a speech model). Most meeting tools export a `.vtt` or `.srt`, and
those are faster and usually more accurate.

To check the pipeline without spending anything, `--fake` runs it with a
stand-in model:

```bash
specto run walkthrough.mp4 --transcript walkthrough.vtt --fake
```

## How it works

1. **Ingest.** ffmpeg finds the moments the screen changed and saves a still
   image for each. Near-identical images are dropped. The transcript is read
   (or made), and each sentence is matched to the image that was showing when
   it was said.
2. **Extract.** Claude reads the images and the words in batches of eight
   frames and writes down the screens, fields, actions and candidate
   requirements it sees, keeping the same screen names across batches. One
   final pass over the whole session merges those notes into requirements with
   acceptance criteria and a list of questions.
3. **Export.** The notes become the workbook and the report, with every row
   pointing at its frame.

Each stage saves its result in the output folder. Running the same command
again skips the stages already done, so a prompt change re-runs only the
extraction, and a workbook layout change re-runs only the export
(`specto export out/walkthrough`). `--force` redoes everything.

## Options

| Flag | Meaning | Default |
|---|---|---|
| `--transcript FILE` | `.vtt`, `.srt`, timestamped `.txt`, or a meeting-tool `.json` | transcribe locally |
| `--out DIR` | where to write | `out/<video name>` |
| `--model ID` | Claude model | `claude-opus-5` |
| `--effort` | how hard the model thinks: low, medium, high, xhigh, max | `high` |
| `--frames-per-call N` | images per model call | 8 |
| `--max-frames N` | cap on still images kept | 120 |
| `--scene-threshold X` | how big a change counts as a new screen, 0 to 1 | 0.3 |
| `--ingest-only` | stop after frames and transcript | |
| `--fake` | stand-in model, no key needed | |
| `--force` | redo every stage | |

## What it costs

A one-hour walkthrough typically yields 60 to 120 still images. Each image is
scaled to 1280 pixels wide and costs roughly 1,500 tokens to read, and the
system prompt is cached between calls. Expect a few dollars per hour of
recording on Claude Opus 5; `--effort medium` and a lower `--max-frames` cut
that further.

## Limits

- Recorded files only. Live capture during a call is the next step (see
  `PLAN.md`).
- It writes a first draft. The Requirements sheet carries a confidence column:
  "high" means the expert said it plainly, "low" means it was inferred from
  the screen. Read the low ones with care.
- Small text on a high-resolution screen may be unreadable at 1280 pixels
  wide; raise `max_width` in `specto/ingest.py` if fields are being missed.
- Speaker names appear only when the transcript file carries them.
- Screen changes are spotted by a change in brightness. Two screens with the
  same layout and brightness but different colours can be missed; lower
  `--scene-threshold` if that happens.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The tests build a small synthetic video, run every stage with a stand-in model,
and open the resulting workbook. They need no network and no API key.

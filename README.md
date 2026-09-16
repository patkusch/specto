<div align="center">

# specto

### Requirements from a screen walkthrough

**An expert talks through the system for two minutes.**
**specto writes the requirements, the acceptance criteria, and the seventeen questions nobody asked.**

<br/>

[![specto turning a recorded walkthrough into ranked questions and requirements](./docs/demo.gif)](./docs/demo.gif)

**A real reading of the example recording.** Every row linked to the frame it came from.
[The thirty-second version](#the-thirty-second-version) · [Run it yourself](#run-it-yourself) · [Scoreboard](docs/scoreboard.html)

<br/>

[![Model](https://img.shields.io/badge/Claude_Opus_5_or_Gemini-1A1A1A?style=for-the-badge)](#using-gemini-instead-of-claude)
[![License](https://img.shields.io/badge/License-MIT-1A1A1A?style=for-the-badge)](./LICENSE)
[![Recall](https://img.shields.io/badge/answer--key_recall-0.97_to_1.00-2ea043?style=for-the-badge)](#does-it-work)
[![Tests](https://img.shields.io/badge/offline_tests-581-2ea043?style=for-the-badge)](#development)
[![CI](https://github.com/patkusch/specto/actions/workflows/ci.yml/badge.svg)](https://github.com/patkusch/specto/actions/workflows/ci.yml)

</div>

---

## The thirty-second version

An onboarding officer records a two-minute walkthrough of the customer tool. At 1:02, on the Review and Submit screen, she says:

> *"You read it through, and if it all looks right you press Submit for approval."*

The frame on screen at that moment shows the ID document still marked **Pending check**, with Submit available. Nobody mentions that. specto ranks it as the first question to ask, because two requirements cannot be finalised until it is answered:

> **Q010 · validation rule · holds up R015, R018 · frame 3 at 01:02**
> Can a record be submitted for approval, and approved, while its ID document is still Pending check? The record on screen was submitted with the ID still pending.

Twenty-four seconds later she says *"Only team leads can approve. The Approve button doesn't show for the rest of us."* The frame shows two green **Approve** buttons while signed in as an Onboarding Officer. specto writes the rule she stated as requirement R018, and the acceptance criterion that frame fails:

> **AC027** · Given a user in the Onboarding Officer role is signed in, when the user opens the Approval Queue, then no row shows an Approve button.

A tester running that criterion against the screen finds the contradiction in one step. An earlier reading of the same recording raised it as a question outright; readings vary, which is why every row carries its frame so a reviewer can check.

<div align="center">

[![The findings column: questions ranked by how many requirements each holds up](./docs/questions.png)](./docs/questions.png)

</div>

---

## What it produces

One workbook and one web page a delivery team can build from:

| Sheet | What is in it |
|---|---|
| Journey | The process step by step, one row per screen visited |
| Screens | Every screen seen, what it is for, and a picture of it |
| Data Fields | Every box, dropdown and column seen or mentioned, with the values on screen |
| Actions | Every button and link the expert used, and where it led |
| Requirements | What the system must do, in the expert's own words, with a picture of the moment |
| Acceptance Criteria | How to check each requirement is met (Given / When / Then) |
| SME Questions | What the expert did not say and someone must ask before building, ranked by what each holds up |
| Transcript | Everything said, with the time and the screen that was showing |
| Glossary | Every role, screen, field and button named, where it first appeared, and a Definition column to fill in |
| Gaps | What the analysis does not cover yet: screens with no requirement, fields never mentioned, requirements with no criteria |
| Personal Data | Every email, phone number, postcode, date of birth, card or account number, name and address that appeared, masked, with the frame |

Plus a single web page with the frames inside it, a Markdown report, a screen-flow picture, and import files for Jira and Azure DevOps.

<div align="center">

[![The finished dashboard: frame, transcript, ranked questions, requirements and the counters](./docs/dashboard.png)](./docs/dashboard.png)

</div>

581 offline tests. Runs without an API key. Nothing leaves your machine except the frames and words you choose to send to a model service.

---

## Does it work?

The example recording has been read twice by Claude through the
bring-your-own-model path (the agents in a Claude Code session acting as
the model, so no API key was involved), once before and once after the
sample customer's details were changed. Against the hand-written answer
key:

| What the key asks for | First reading | Second reading |
|---|---|---|
| Screens | 6 of 6 | 6 of 6 |
| Fields | 14 of 14 | 14 of 14 |
| Actions | 4 of 4 | 4 of 4 |
| Requirements | 6 of 7 | 7 of 7 |
| Questions | 5 of 5 | 5 of 5 |
| Overall recall | 0.97 | 1.00 |

The second reading found 6 screens, 44 fields, 25 requirements, 33
acceptance criteria and 20 questions, matching every item in the answer
key. In both readings the model noticed things the narration never said:
the Approve button is visible on screen while the expert says only team
leads see it, and a record can be submitted while its ID document is still
"Pending check". Both became questions, each naming the requirements it
holds up. The current reference result is in `examples/onboarding/reference/`.

<div align="center">

[![Recall against the answer keys and the cost and speed counters](./docs/scoreboard.png)](./docs/scoreboard.png)

</div>

A one-page scoreboard for a business reader, with every figure marked
measured or assumed, a savings calculator, a comparison with named
neighbours and the roadmap: [docs/scoreboard.html](docs/scoreboard.html).

The second example, the complaints tool recorded inside a meeting frame,
was read once the same way: 6 screens, 42 fields, 47 requirements, 60
criteria and 23 questions, and every item in its answer key was found
(recall 1.00). Its result is in `examples/claims/reference/`.

The same stages have not yet been run through the API itself, so the first
run with a key should be on an example, and the score compared with its
reference folder.

## Run it yourself

To see real output before you have a key, this one command does it:

```bash
pip install -e ".[ocr]"
specto demo --open
```

It runs the onboarding example recording end to end, answers it with a
reading Claude gave earlier (saved in `examples/onboarding/reference/`), and
opens the report in your browser. Nothing is sent anywhere and no key is
needed. `specto demo --example claims` does the same for the second example.
The workbook and report land in `out/demo-onboarding/`.

To run it on your own recording:

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
answer cells and a status dropdown you can fill in during the follow-up
call; they are saved in that browser as you go, and an Export answers
button turns them into a file that flows back into the workbook (see
[After the follow-up call](#after-the-follow-up-call)). Print to PDF instead
to keep a paper copy.

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

Transcript files from meeting tools (`.vtt`, `.srt`, `.json`) carry no word
times, so when a sentence runs across a screen change specto spreads its
words evenly over the sentence's time (a long word gets a little more) and
cuts it there, which lands within a word or two of the right place. They are
still the faster and usually more accurate route when you have them.

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

### With Docker

No local Python needed. From the repo folder:

```bash
docker build -t specto .
docker run --rm -v "$PWD:/work" specto demo
```

The output lands in `out/demo-onboarding/` on your machine. Your folder is
mounted at `/work`, so a run on your own recording reads and writes there.
Pass a key into the container with `-e`; with no value after the name it
copies the one already set in your shell:

```bash
docker run --rm -v "$PWD:/work" -e ANTHROPIC_API_KEY specto run walkthrough.mp4 --transcript walkthrough.vtt
docker run --rm -v "$PWD:/work" -e GEMINI_API_KEY specto run walkthrough.mp4 --transcript walkthrough.vtt --provider gemini
```

With no command the container runs `specto doctor`.

## Other ways in

The same pipeline runs from more than a finished recording.

### A meeting recording with the share in the middle

A Teams or Zoom recording shows the shared window inside a border with a
toolbar and a strip of faces. `--crop auto` finds the part of the picture
that actually changes over the recording and keeps only that, so the model
reads the shared window and not the meeting around it. If it picks the
wrong area, pass the box yourself as `--crop x,y,w,h` in pixels.

A second example recording, `examples/claims/`, is exactly that: a
complaints-handling tool with table-heavy screens, recorded inside a
Teams-style frame with participant tiles and a toolbar. `--crop auto` finds
the shared window in it to the pixel:

```bash
specto run examples/claims/walkthrough.mp4 --transcript examples/claims/walkthrough.vtt --crop auto --fake
```

### No recording, just screenshots

```bash
specto run shots/ --transcript notes.md
```

When the expert sent a folder of screenshots and some written notes instead
of a recording, point specto at the folder. Times come from the file names
when they carry one (`shot_12.png`, a date-time), otherwise the screenshots
are spaced ten seconds apart in name order. Notes without timestamps are
spread over the screenshots in order, one paragraph per picture.

### During a call, not after it

```bash
specto live --out out/todays-call
```

Live mode runs on macOS, Windows and Linux. It grabs the shared screen every
three seconds and records the microphone in thirty-second pieces, keeps only
the moments the screen changed, and every five minutes runs the whole
session so far through the same analysis. `out/todays-call/live_questions.md`
holds just the open questions, most blocking first, so it can sit in a
window and be asked before the expert leaves. Press Ctrl-C to stop; the
full workbook and reports are written at the end. If the folder already
exists the session is resumed. To try it without a call,
`--replay DIR --transcript FILE` feeds a folder of screenshots named
`shot_<seconds>.png` through the same path.

For the screen, install the small `mss` library with
`pip install -e ".[live]"`; it works on all three systems. Without it specto
falls back to what the system already has: `screencapture` on macOS,
PowerShell on Windows, and `grim` (Wayland) or ImageMagick's `import` (X11)
on Linux. For the microphone, the bundled ffmpeg uses the input each system
has; on Windows the first microphone it lists is used and the log says
which, and `--audio-device` picks another.

On macOS, give your terminal Screen Recording and Microphone permission
first (System Settings, Privacy & Security). Without Screen Recording, live
mode stops with a line saying so rather than recording an empty desktop.
`specto doctor` shows which screen backend and microphone input will be
used on your machine and, on a Mac, whether the permission is granted.

### No key at all: answer the requests yourself

```bash
specto run walkthrough.mp4 --transcript walkthrough.vtt --ingest-only
specto requests out/walkthrough      # writes requests/chunk_01.json, chunk_02.json ...
```

Some teams cannot put an API key on a machine but can paste into a chat
window or run a model through their own gateway. Each request file holds
the instructions, the frames to attach (by file name), the words spoken,
and the exact shape the answer must take. Put the model's answer in
`chunk_01.response.json`, run `specto requests out/walkthrough --regenerate 2`
so the next request knows the screens already named, and carry on. When
every chunk is answered, `specto requests` writes the final merge request;
answer it, then:

```bash
specto load out/walkthrough          # reads the answers, writes every output
```

`specto status out/walkthrough` says what is answered and what comes next.

### Using Gemini instead of Claude

```bash
export GEMINI_API_KEY=...   # or GOOGLE_API_KEY; a free key comes from https://aistudio.google.com/apikey
specto run walkthrough.mp4 --transcript walkthrough.vtt --provider gemini --model gemini-2.5-pro
```

The same stages run against Google's Gemini; `gemini-2.5-flash` is the
cheaper choice for the frame-reading pass. If the first call fails with a
403 saying the API "has not been used in project", open that Google Cloud
project's APIs & Services, Library, enable the Generative Language API, and
run it again. An existing Google API key from another product (Maps,
YouTube) works once that API is enabled on its project.

## Batch mode: watch a shared folder

```bash
specto watch shared/incoming --out shared/out
```

For a team that gets a steady stream of walkthroughs: point specto at one
shared folder and it processes whatever lands there, so nobody has to run a
command by hand for every recording. Drop in a finished video, or a folder
of screenshots, and specto notices it within ten seconds, then writes the
same workbook, report and questions `specto run` would, in its own folder
under `--out`. An item specto has already finished is left alone, so
stopping and restarting `watch` does not redo work; dropping in a replacement
recording under the same name does get reprocessed. One bad file (a corrupt
recording, say) is logged and skipped without holding up the rest of the
folder. Add `--once` to process whatever is there right now and exit, for a
scheduled job rather than a machine left running, and `--fake` to check the
setup costs nothing before pointing it at a real model.

## After the follow-up call

Two ways to get the expert's answers back into the analysis; use whichever
one the answers were typed into.

**Typed into the workbook.** Type the expert's answers into the Answer
column of the SME Questions sheet (and "not needed" in Status for the ones
that no longer matter), then:

```bash
specto answers out/walkthrough
```

**Typed into the web page.** `report.html` can be filled in instead, by
whoever has the file, without needing the workbook at all. Open it, type
each answer into its Answer cell and pick a Status from the dropdown next
to it; both are saved in that browser as they are typed, so the page can be
closed and reopened later without losing anything. When every answer is in,
click **Export answers** at the top of the SME questions table, save the
file it downloads next to `analysis.xlsx` as `answers.json`, and run:

```bash
specto answers out/walkthrough --from-html answers.json
```

Either command reads the answers back and rewrites every output with them.
Then, whichever route was used:

```bash
specto resolve out/walkthrough
```

This sends only the answered questions to the model and adds what the
answers establish: new requirements with their source marked as the answer,
changed wording on existing ones with the reason recorded, criteria for
each, and any follow-up questions the answers raised. Running it again only
sends newly answered questions.

## Before you share it

An expert walking through a real system shows real customer records. The
Personal Data sheet lists what personal data the recording captured and on
which frame, with the values masked. Check it before the workbook, the
report or the frames folder is shared or stored. Then:

```bash
specto redact out/walkthrough
```

paints over every email, phone number, postcode, date of birth, card or
account number, name and address on the still frames, masks the same values
inside the outputs, and rewrites the workbook and reports. The untouched
frames are kept under `frames/original/` until you delete that folder (do
that before the output leaves the team); `specto restore` puts them back.
Needs the OCR extra.

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
(`specto export out/walkthrough`). A model run that dies halfway (network,
Ctrl-C) saves what it has read so far and picks up from there next time.
`--force` redoes everything, including the model reads.

## Reference

### Commands

| Command | What it does |
|---|---|
| `specto run VIDEO` | the whole pipeline on a recording |
| `specto live --out DIR` | screen and microphone during a call, questions every five minutes |
| `specto watch FOLDER --out DIR` | process every recording a team drops into a shared folder |
| `specto export DIR` | rebuild every output from a finished analysis |
| `specto score DIR KEY` | compare an output with an answer key |
| `specto demo [--example onboarding\|claims] [--open]` | run a real example end to end with a saved reading, no key and no model call |
| `specto doctor` | what is installed and what is missing |
| `specto requests DIR`, `specto load DIR`, `specto status DIR` | write the model requests as files, read the answers back, see what is left; no key needed |
| `specto merge DIR DIR... --out DIR` | several sessions into one workbook: same screens, requirements and questions folded together, no model call |
| `specto redact DIR` | paint over the personal data on the frames and mask it in the outputs; `specto restore DIR` undoes it |
| `specto answers DIR [--from-html FILE]` | read the Answer and Status columns typed into the workbook back in, or, with `--from-html`, the answers.json exported from report.html |
| `specto resolve DIR` | turn the answered questions into requirements and criteria, and raise any follow-ups |

### Options

| Flag | Meaning | Default |
|---|---|---|
| `--transcript FILE` | `.vtt`, `.srt`, timestamped `.txt`, or a meeting-tool `.json` | transcribe locally |
| `--out DIR` | where to write | `out/<video name>` |
| `--provider claude|gemini` | which model service to call | `claude` |
| `--model ID` | model id for the provider | `claude-opus-5` |
| `--reader-model ID` | a cheaper model for reading the frames, e.g. `claude-sonnet-5`; the final merge still uses `--model` | same as `--model` |
| `--effort` | how hard the model thinks: low, medium, high, xhigh, max | `high` |
| `--frames-per-call N` | images per model call | 8 |
| `--max-frames N` | cap on still images kept; over the cap, the frames that changed least from their neighbour are dropped first | 240 |
| `--crop x,y,w,h` or `--crop auto` | keep only part of the picture; `auto` finds the shared window and drops the meeting border, toolbar and gallery strip | whole picture |
| `--detect hash|scene` | find screen changes by image fingerprint, or by ffmpeg brightness | `hash` |
| `--hash-distance N` | how different a frame must be to count as new; lower catches typed text | 8 |
| `--sample-fps X` | frames looked at per second in hash mode | 1 |
| `--scene-threshold X` | brightness change that counts as a new screen, scene mode only | 0.3 |
| `--ocr / --no-ocr` | read the text on each frame for the model | on |
| `--ingest-only` | stop after frames and transcript | |
| `--estimate` | print the expected cost for each model and stop | |
| `--max-cost USD` | stop before the model if the estimate is above this | |
| `--fake` | stand-in model, no key needed | |
| `--force` | redo every stage | |

### Checking the output against an answer key

`examples/onboarding/expected.json` lists what a good analysis of the example
must contain: screen names, field labels, and keyword groups for actions,
requirements and questions. After a run, the score command reports how much
of it was found and what was missed:

```bash
specto score out/walkthrough examples/onboarding/expected.json --min 0.7
```

This is how a prompt change is judged: run, score, compare. Write an answer
key for your own recording the same way.

### What it costs

Before any real run specto prints what it expects to spend, and
`--estimate` prints the figure for each model and stops:

```bash
specto run walkthrough.mp4 --transcript walkthrough.vtt --estimate
```

`--max-cost 5` stops before the model whenever the estimate is above five
dollars, so a long recording cannot run up a bill by accident.

`specto doctor` lists what is installed and what is missing (ffmpeg, the
speech model, OCR, the API key) and what to do about each.

A one-hour walkthrough typically yields 100 to 130 still images: one per
screen, plus two or three for every value typed and two for every popup.
Ingest itself takes about twenty seconds for an hour of full-size video on
a laptop. Measured on a synthetic hour with 40 screens, the model cost
estimate came to between $1.15 and $1.60 on Claude Opus 5. Each image is
scaled to 1280 pixels wide and costs roughly 1,500 tokens to read, and the
system prompt is cached between calls. Expect a few dollars per hour of
recording on Claude Opus 5; `--effort medium` and a lower `--max-frames` cut
that further.

### Limits

- On Linux, `grim` and `import` capture every monitor together, so `--display` only selects a monitor when `mss` is installed. Each analysis round reuses what the model
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

### Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

The tests build a small synthetic video, run every stage with a stand-in model,
and open the resulting workbook. They need no network and no API key.

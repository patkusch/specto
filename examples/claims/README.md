# Claims example

A second stand-in for a real screen share: an expert talks through a fake complaints-handling tool called Meridian Complaints, six screens from the Complaints Inbox to the Dashboard, with real spoken audio (the Mac's Daniel voice). Nothing in it is a real person or company; every name, reference, date, amount and phone number comes from one seeded random generator in `make_example.py`.

It differs from the onboarding example in two ways. The screens are tables, a timeline, radio buttons, a calculator, a letter and a bar chart rather than forms. And the recording is not full-screen: each 1280x720 page sits inside a 1600x900 Teams-style call with a dark border, a top bar with a meeting timer, four participant tiles (one with a blinking mic dot) and a toolbar. The shared window is the 1280x720 area at (160, 60), which is what `--crop auto` has to find.

`walkthrough.mp4` (about 2 MB, 178 seconds) is the recording, `walkthrough.vtt` is the transcript with the same timing, and `expected.json` is the hand-written answer key: the screens, fields, actions, requirements and open questions a good run should find.

To rebuild: `.venv/bin/pip install playwright && .venv/bin/playwright install chromium`, then `.venv/bin/python examples/claims/make_example.py` from the repo root. It rewrites the pages in `app/`, prints the timeline and overwrites the mp4 and vtt.

To run specto on it: `specto run examples/claims/walkthrough.mp4 --transcript examples/claims/walkthrough.vtt --crop auto --fake` (drop `--fake` to use an API key). Measured on 2026-09-14: the crop detector found the 1280x720 window at 160,60 exactly and the hash detector kept 6 keyframes, one per screen.

## Reference result

`reference/` holds a real reading of this recording by Claude through the
bring-your-own-model path: 6 screens, 42 fields, 46 requirements, 60
criteria and 23 questions, with overall recall 1.00 against `expected.json`.
Use it to compare a new prompt or model.

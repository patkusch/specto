# Deliveries example

A stand-in for a phone-app screen recording: a dispatcher talks a new driver through an invented courier app, Harbourline Drivers, six screens from Today's runs to the End of day summary, with real spoken audio (the Mac's Moira voice). Nothing in it is a real person, firm or postcode.
This is the held-out example. No specto prompt was ever tuned against it, and `expected.json` was written from the narration script and screen designs in `make_example.py` before any model read the recording, so a reading of it is an honest test of how well the tool generalises.
It differs from the onboarding and claims examples in shape: a portrait 540x1170 phone recording (a 390x844 screen rendered at 2x in mobile emulation, dark theme), not a landscape desktop, with no meeting frame around it.
The narration states seven rules, leaves two things vague (why the time window turns red, where the cash figure comes from), says every stop has a phone number while the Stop detail screen shows one that has none, and never mentions the orange "Hazardous" badge on a parcel; the last two are the questions that can only come from looking at the screen.
`walkthrough.mp4` (about 1.7 MB, 168 seconds) is the recording, `walkthrough.vtt` is the transcript with the same timing (17 lines), and `expected.json` is the hand-written answer key. Every sample value comes from `sample_data()` in `make_example.py`, seeded with `random.Random(20260918)`.
To rebuild: `.venv/bin/pip install playwright && .venv/bin/playwright install chromium`, then `.venv/bin/python examples/deliveries/make_example.py` from the repo root. It rewrites `app/`, prints the timeline and overwrites the mp4, vtt and `frames/`.
`--crop auto` leaves this recording whole: it has no border, and the detector only crops when two opposite sides have a wide static margin (this one has a blank strip at the bottom only).

## Reference result (held-out)

`reference/` holds the first reading of this recording, made blind: the
reading agents were told not to open the answer key, the script that built
the recording, or any other example's reference. No prompt was tuned against
this example. Overall recall against `expected.json` was **0.94**: screens
6 of 6, actions 4 of 4, requirements 7 of 7, fields 12 of 13, questions 4 of
5. Both questions that can only come from looking at the screen were found
(the stop with no phone number, the unmentioned HAZARDOUS badge).

The two misses, described honestly:

- **Field:** the free-text box on the failed-delivery screen was found, under
  its on-screen label "Anything else the depot should know?". The key looks
  for the words note, free text, comment or detail, so it counts as missed.
  That is a wording limit of the key, and the key was not changed after the
  reading.
- **Question:** the key wanted "do parcels of £100 or under need a
  signature?". The reading asked about the boundary instead ("does a parcel
  worth exactly £100 need a photo and signature?"), which is close but does
  not match.

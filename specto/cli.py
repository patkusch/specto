"""Command line entry point.

    specto run walkthrough.mp4 --out out/walkthrough
    specto run walkthrough.mp4 --transcript walkthrough.vtt --out out/walkthrough
    specto export out/walkthrough        # rebuild the workbook from analysis.json
    specto score out/walkthrough expected.json   # compare with an answer key
    specto doctor                        # what is installed, what is missing
    specto merge out/day1 out/day2 --out out/all   # several sessions, one workbook
    specto requests out/walkthrough      # write the model requests as files (no key needed)
    specto load out/walkthrough          # read the answers back and write every output
    specto redact out/walkthrough        # paint over personal data on the frames
    specto answers out/walkthrough       # read the answers typed into the workbook
    specto resolve out/walkthrough       # answered questions become requirements
    specto live --out out/call           # during a call: screen + mic, questions every 5 minutes

Each stage saves its result in the output folder, so running the same command
again picks up where it left off. Pass --force to redo everything.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .model import Analysis, Recording


def _load(path: Path, model):
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def make_caller(args: argparse.Namespace, model: str | None = None):
    """The model caller for the chosen provider, or None (with a message) when no key is set."""
    model = model or args.model
    if getattr(args, "provider", "claude") == "gemini":
        from .gemini import GeminiCaller, gemini_available

        if not gemini_available():
            print("GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. Get a key at https://aistudio.google.com/apikey, "
                  "or pass --fake to try without the model.", file=sys.stderr)
            return None
        if model.startswith("claude-"):
            model = "gemini-2.5-pro"
        return GeminiCaller(model=model)
    from .extract import ClaudeCaller

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Export it, pass --provider gemini with a Gemini key, "
              "or pass --fake to try without the model.", file=sys.stderr)
        return None
    return ClaudeCaller(model=model, effort=args.effort)


def cmd_run(args: argparse.Namespace) -> int:
    from .export import export_all
    from .extract import extract
    from .ingest import ingest

    source = Path(args.video)
    out_dir = Path(args.out or Path("out") / (source.name or "run"))
    out_dir.mkdir(parents=True, exist_ok=True)

    if source.is_dir():
        from .ingest import ingest_folder

        recording = ingest_folder(source, out_dir, transcript_path=args.transcript,
                                  force=args.force, hash_distance=args.hash_distance)
        print(f"ingest: {len(recording.keyframes)} screenshots kept, {len(recording.segments)} notes or "
              f"transcript segments, laid out over {recording.duration:.0f}s")
    else:
        crop = None
        if args.crop:
            if args.crop.strip().lower() == "auto":
                crop = "auto"
            else:
                try:
                    crop = tuple(int(v) for v in args.crop.split(","))
                except ValueError:
                    crop = ()
                if len(crop) != 4:
                    print("--crop must be x,y,w,h in pixels or 'auto'", file=sys.stderr)
                    return 2
        recording = ingest(
            source,
            out_dir,
            transcript_path=args.transcript,
            whisper_model=args.whisper_model,
            force=args.force,
            max_frames=args.max_frames,
            scene_threshold=args.scene_threshold,
            detect=args.detect,
            hash_distance=args.hash_distance,
            sample_fps=args.sample_fps,
            crop=crop,
        )
        print(f"ingest: {len(recording.keyframes)} frames, {len(recording.segments)} transcript segments, "
              f"{recording.duration:.0f}s of video")

    ocr_text = None
    if args.ocr:
        from .ocr import ocr_recording
        ocr_text = ocr_recording(recording, out_dir, force=args.force) or None

    if args.ingest_only:
        print(f"stopped after ingest; see {out_dir / 'recording.json'}")
        return 0

    from .estimate import compare_models, estimate, format_comparison, format_estimate

    if not args.fake and not (out_dir / "analysis.json").exists() or args.estimate:
        print(format_estimate(estimate(recording, out_dir, model=args.model,
                                       frames_per_call=args.frames_per_call, ocr_text=ocr_text)))
    if args.estimate:
        print(format_comparison(compare_models(recording, out_dir, frames_per_call=args.frames_per_call,
                                               ocr_text=ocr_text)))
        print("stopped before the model; drop --estimate to run it")
        return 0
    if args.max_cost is not None and not (out_dir / "analysis.json").exists():
        expected = estimate(recording, out_dir, model=args.model, frames_per_call=args.frames_per_call,
                            ocr_text=ocr_text).total_cost_usd
        if expected > args.max_cost:
            print(f"stopped: the estimate is ${expected:.2f} and --max-cost is ${args.max_cost:.2f}. "
                  f"Raise the cap, lower --max-frames, or pick a cheaper --model or --reader-model.",
                  file=sys.stderr)
            return 3

    reader = None
    if args.fake:
        from .fake import FakeCaller
        caller = FakeCaller()
    else:
        caller = make_caller(args)
        if caller is None:
            return 2
        if args.reader_model and args.reader_model != args.model:
            reader = make_caller(args, args.reader_model)

    if args.force and (out_dir / "chunk_readings.json").exists():
        (out_dir / "chunk_readings.json").unlink()
    analysis = extract(recording, out_dir, caller=caller, force=args.force,
                       frames_per_call=args.frames_per_call, ocr_text=ocr_text,
                       reuse_readings=True, reader=reader)
    if analysis.usage:
        u = analysis.usage
        print(f"extract: {u.calls} model calls, {u.input_tokens} in / {u.output_tokens} out tokens, "
              f"{u.cache_read_input_tokens} read from cache")
    print(f"extract: {len(analysis.screens)} screens, {len(analysis.fields)} fields, "
          f"{len(analysis.requirements)} requirements, {len(analysis.acceptance_criteria)} criteria, "
          f"{len(analysis.questions)} questions")

    paths = export_all(analysis, recording, out_dir)
    for name, p in paths.items():
        print(f"wrote {name}: {p}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .export import export_all

    out_dir = Path(args.out_dir)
    analysis = _load(out_dir / "analysis.json", Analysis)
    recording = _load(out_dir / "recording.json", Recording)
    for name, p in export_all(analysis, recording, out_dir).items():
        print(f"wrote {name}: {p}")
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    from .live import capture, replay

    out_dir = Path(args.out)
    if args.fake:
        from .fake import FakeCaller
        caller = FakeCaller()
    else:
        caller = make_caller(args)
        if caller is None:
            return 2

    if args.replay:
        replay(out_dir, args.replay, args.transcript, caller, every_seconds=args.every)
    else:
        capture(out_dir, interval=args.interval, audio_chunk_seconds=args.audio_chunk,
                every_seconds=args.every, caller=caller, display=args.display,
                audio_device=args.audio_device, whisper_model=args.whisper_model)
    print(f"live outputs are in {out_dir}; open {out_dir / 'live_questions.md'} for the questions")
    return 0


def cmd_answers(args: argparse.Namespace) -> int:
    from .answers import import_answers
    from .export import export_all

    out_dir = Path(args.out_dir)
    result = import_answers(out_dir, args.xlsx)
    print(f"answers: {result.changed} question(s) updated" + (f"; unknown ids ignored: {', '.join(result.unknown_ids)}" if result.unknown_ids else ""))
    analysis = _load(out_dir / "analysis.json", Analysis)
    recording = _load(out_dir / "recording.json", Recording)
    for name, p in export_all(analysis, recording, out_dir).items():
        print(f"wrote {name}: {p}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    from .export import export_all
    from .resolve import resolve_dir

    out_dir = Path(args.out_dir)
    if args.fake:
        from .fake import FakeCaller
        caller = FakeCaller()
    else:
        caller = make_caller(args)
        if caller is None:
            return 2
    response = resolve_dir(out_dir, caller=caller)
    print(f"resolve: {len(response.new_requirements)} new requirement(s), {len(response.updated_statements)} updated, "
          f"{len(response.new_acceptance_criteria)} new criteria, {len(response.follow_up_questions)} follow-up question(s)")
    analysis = _load(out_dir / "analysis.json", Analysis)
    recording = _load(out_dir / "recording.json", Recording)
    for name, p in export_all(analysis, recording, out_dir).items():
        print(f"wrote {name}: {p}")
    return 0


def cmd_redact(args: argparse.Namespace) -> int:
    from .export import export_all
    from .redact import redact_dir

    out_dir = Path(args.out_dir)
    report = redact_dir(out_dir, style=args.style)
    kinds = ", ".join(f"{n} {k}" for k, n in sorted(report.kinds.items())) or "nothing found"
    print(f"redact: {report.boxes_painted} spot(s) painted on {report.frames_touched} frame(s), "
          f"{report.text_replacements} text value(s) masked ({kinds})")
    print(f"the untouched frames are kept under {out_dir / 'frames' / 'original'}; delete that folder and "
          f"ocr_lines.json before the output leaves the team, or run 'specto restore' to put them back")
    analysis = _load(out_dir / "analysis.json", Analysis)
    recording = _load(out_dir / "recording.json", Recording)
    for name, p in export_all(analysis, recording, out_dir).items():
        print(f"wrote {name}: {p}")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from .redact import restore_dir

    n = restore_dir(Path(args.out_dir))
    print(f"restore: {n} frame(s) put back from frames/original" if n else "restore: nothing to put back")
    return 0


def _ocr_text_from(out_dir: Path) -> dict[int, str] | None:
    from .pii import load_ocr_text

    return load_ocr_text(out_dir) or None


def cmd_requests(args: argparse.Namespace) -> int:
    from .byo import ByoError, dump_consolidate_request, dump_requests, regenerate_chunk_request, requests_dir, status

    out_dir = Path(args.out_dir)
    try:
        recording = _load(out_dir / "recording.json", Recording)
        if args.regenerate:
            regenerate_chunk_request(out_dir, args.regenerate)
        elif not (requests_dir(out_dir) / "manifest.json").exists():
            dump_requests(recording, out_dir, frames_per_call=args.frames_per_call, ocr_text=_ocr_text_from(out_dir))
        else:
            import json as _json

            chunk_count = _json.loads((requests_dir(out_dir) / "manifest.json").read_text())["chunk_count"]
            answered = all((requests_dir(out_dir) / f"chunk_{n:02d}.response.json").exists()
                           for n in range(1, chunk_count + 1))
            if answered:
                dump_consolidate_request(recording, out_dir)
            else:
                dump_requests(recording, out_dir, frames_per_call=args.frames_per_call, ocr_text=_ocr_text_from(out_dir))
        print(status(out_dir))
    except ByoError as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    from .byo import ByoError, load_responses
    from .export import export_all

    out_dir = Path(args.out_dir)
    try:
        recording = _load(out_dir / "recording.json", Recording)
        analysis = load_responses(recording, out_dir)
    except ByoError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"loaded: {len(analysis.screens)} screens, {len(analysis.fields)} fields, "
          f"{len(analysis.requirements)} requirements, {len(analysis.acceptance_criteria)} criteria, "
          f"{len(analysis.questions)} questions")
    for name, p in export_all(analysis, recording, out_dir).items():
        print(f"wrote {name}: {p}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from .byo import status

    print(status(Path(args.out_dir)))
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    from .export import export_all
    from .merge import merge_dirs, merge_report

    out_dir = Path(args.out)
    analysis, recording = merge_dirs(args.sources, out_dir)
    print(merge_report(args.sources, analysis))
    for name, p in export_all(analysis, recording, out_dir).items():
        print(f"wrote {name}: {p}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import main as doctor_main

    return doctor_main([])


def cmd_score(args: argparse.Namespace) -> int:
    from .score import main as score_main

    return score_main([args.out_dir, args.expected, "--min", str(args.min)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specto", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="watch a recording and write the workbook")
    run.add_argument("video", help="video file (mp4, mov, mkv, webm), or a folder of screenshots (png, jpg, webp)")
    run.add_argument("--transcript", help="transcript file (.vtt, .srt, .txt, .json); transcribed locally if omitted. "
                     "With a screenshot folder, plain notes (.txt or .md without timestamps) are spread over the screenshots")
    run.add_argument("--out", help="output folder (default: out/<video name>)")
    run.add_argument("--model", default="claude-opus-5", help="Claude model id (default: claude-opus-5)")
    run.add_argument("--provider", default="claude", choices=["claude", "gemini"], help="which model service to call (default claude)")
    run.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--reader-model", help="a cheaper model for reading the frames, e.g. claude-sonnet-5; the merge still uses --model")
    run.add_argument("--frames-per-call", type=int, default=8, help="frames sent per model call (default 8)")
    run.add_argument("--max-frames", type=int, default=240, help="cap on still frames kept; the least-changed frames go first (default 240)")
    run.add_argument("--crop", metavar="x,y,w,h|auto", help="keep only this part of the picture (pixels), or 'auto' to find the shared window and drop the border, toolbar and gallery strip around it")
    run.add_argument("--detect", default="hash", choices=["hash", "scene"],
                     help="how screen changes are found: image hash (default) or ffmpeg brightness")
    run.add_argument("--hash-distance", type=int, default=8, help="how different a frame must be to count as new (default 8; lower catches typed text)")
    run.add_argument("--sample-fps", type=float, default=1.0, help="frames looked at per second in hash mode (default 1)")
    run.add_argument("--scene-threshold", type=float, default=0.3, help="ffmpeg scene-change threshold 0..1, scene mode only (default 0.3)")
    run.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=True,
                     help="read the text on each frame and show it to the model (needs pip install 'specto[ocr]')")
    run.add_argument("--whisper-model", default="base", help="faster-whisper model size when transcribing locally")
    run.add_argument("--ingest-only", action="store_true", help="stop after frames and transcript")
    run.add_argument("--estimate", action="store_true", help="print the expected cost for each model and stop before calling one")
    run.add_argument("--max-cost", type=float, metavar="USD", help="stop before the model if the estimate is above this many dollars")
    run.add_argument("--fake", action="store_true", help="use a fake model (no API key needed) to check the pipeline")
    run.add_argument("--force", action="store_true", help="redo every stage even if outputs exist")
    run.set_defaults(func=cmd_run)

    exp = sub.add_parser("export", help="rebuild the workbook and report from an output folder")
    exp.add_argument("out_dir")
    exp.set_defaults(func=cmd_export)

    live = sub.add_parser("live", help="watch the shared screen and microphone during a call (macOS)")
    live.add_argument("--out", required=True, help="output folder; an existing one is resumed")
    live.add_argument("--replay", metavar="DIR", help="instead of capturing, feed a folder of screenshots named shot_<seconds>.png")
    live.add_argument("--transcript", help="transcript file to use with --replay")
    live.add_argument("--every", type=float, default=300, help="seconds between analyses (default 300)")
    live.add_argument("--interval", type=float, default=3.0, help="seconds between screenshots (default 3)")
    live.add_argument("--audio-chunk", type=float, default=30, help="seconds of microphone per transcribed chunk (default 30)")
    live.add_argument("--display", type=int, default=1, help="which display to capture (default 1)")
    live.add_argument("--audio-device", default=":0", help="ffmpeg avfoundation audio device (default :0)")
    live.add_argument("--whisper-model", default="base")
    live.add_argument("--model", default="claude-opus-5")
    live.add_argument("--provider", default="claude", choices=["claude", "gemini"], help="which model service to call (default claude)")
    live.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    live.add_argument("--fake", action="store_true", help="stand-in model, no key needed")
    live.set_defaults(func=cmd_live)

    an = sub.add_parser("answers", help="read the Answer and Status columns typed into the workbook back into the analysis")
    an.add_argument("out_dir")
    an.add_argument("--xlsx", help="the workbook with the answers (default: out_dir/analysis.xlsx)")
    an.set_defaults(func=cmd_answers)

    rs = sub.add_parser("resolve", help="turn answered questions into requirements and criteria")
    rs.add_argument("out_dir")
    rs.add_argument("--model", default="claude-opus-5")
    rs.add_argument("--provider", default="claude", choices=["claude", "gemini"], help="which model service to call (default claude)")
    rs.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    rs.add_argument("--fake", action="store_true", help="stand-in model, no key needed")
    rs.set_defaults(func=cmd_resolve)

    rd = sub.add_parser("redact", help="paint over the personal data on the frames and mask it in the outputs")
    rd.add_argument("out_dir")
    rd.add_argument("--style", default="box", choices=["box", "blur"], help="opaque box (default, cannot be undone) or a heavy blur")
    rd.set_defaults(func=cmd_redact)

    rs2 = sub.add_parser("restore", help="put the untouched frames back after a redact")
    rs2.add_argument("out_dir")
    rs2.set_defaults(func=cmd_restore)

    rq = sub.add_parser("requests", help="write the model requests as files, to answer with any model you can reach")
    rq.add_argument("out_dir", help="an output folder after --ingest-only")
    rq.add_argument("--frames-per-call", type=int, default=8)
    rq.add_argument("--regenerate", type=int, metavar="N", help="rebuild chunk N's request using the answers to earlier chunks")
    rq.set_defaults(func=cmd_requests)

    ld = sub.add_parser("load", help="read the answer files back and write every output")
    ld.add_argument("out_dir")
    ld.set_defaults(func=cmd_load)

    st = sub.add_parser("status", help="which requests are written, which are answered, what to do next")
    st.add_argument("out_dir")
    st.set_defaults(func=cmd_status)

    mg = sub.add_parser("merge", help="combine several finished runs into one workbook, no model call")
    mg.add_argument("sources", nargs="+", help="output folders of finished runs, in session order")
    mg.add_argument("--out", required=True, help="folder for the merged result")
    mg.set_defaults(func=cmd_merge)

    doc = sub.add_parser("doctor", help="check what is installed and what is missing")
    doc.set_defaults(func=cmd_doctor)

    sc = sub.add_parser("score", help="compare an output folder with a hand-written answer key")
    sc.add_argument("out_dir")
    sc.add_argument("expected", help="expected.json answer key")
    sc.add_argument("--min", type=float, default=0.0, help="exit 1 if overall recall is below this")
    sc.set_defaults(func=cmd_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

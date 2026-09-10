"""Command line entry point.

    specto run walkthrough.mp4 --out out/walkthrough
    specto run walkthrough.mp4 --transcript walkthrough.vtt --out out/walkthrough
    specto export out/walkthrough        # rebuild the workbook from analysis.json
    specto score out/walkthrough expected.json   # compare with an answer key

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


def cmd_run(args: argparse.Namespace) -> int:
    from .export import export_all
    from .extract import ClaudeCaller, extract
    from .ingest import ingest

    out_dir = Path(args.out or Path("out") / Path(args.video).stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    recording = ingest(
        args.video,
        out_dir,
        transcript_path=args.transcript,
        whisper_model=args.whisper_model,
        force=args.force,
        max_frames=args.max_frames,
        scene_threshold=args.scene_threshold,
        detect=args.detect,
        hash_distance=args.hash_distance,
        sample_fps=args.sample_fps,
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

    if args.fake:
        from .fake import FakeCaller
        caller = FakeCaller()
    else:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set. Export it, or pass --fake to try the pipeline without the model.",
                  file=sys.stderr)
            return 2
        caller = ClaudeCaller(model=args.model, effort=args.effort)

    analysis = extract(recording, out_dir, caller=caller, force=args.force,
                       frames_per_call=args.frames_per_call, ocr_text=ocr_text)
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


def cmd_score(args: argparse.Namespace) -> int:
    from .score import main as score_main

    return score_main([args.out_dir, args.expected, "--min", str(args.min)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specto", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="watch a recording and write the workbook")
    run.add_argument("video", help="video file (mp4, mov, mkv, webm)")
    run.add_argument("--transcript", help="transcript file (.vtt, .srt, .txt, .json); transcribed locally if omitted")
    run.add_argument("--out", help="output folder (default: out/<video name>)")
    run.add_argument("--model", default="claude-opus-5", help="Claude model id (default: claude-opus-5)")
    run.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--frames-per-call", type=int, default=8, help="frames sent per model call (default 8)")
    run.add_argument("--max-frames", type=int, default=120, help="cap on still frames kept (default 120)")
    run.add_argument("--detect", default="hash", choices=["hash", "scene"],
                     help="how screen changes are found: image hash (default) or ffmpeg brightness")
    run.add_argument("--hash-distance", type=int, default=8, help="how different a frame must be to count as new (default 8; lower catches typed text)")
    run.add_argument("--sample-fps", type=float, default=1.0, help="frames looked at per second in hash mode (default 1)")
    run.add_argument("--scene-threshold", type=float, default=0.3, help="ffmpeg scene-change threshold 0..1, scene mode only (default 0.3)")
    run.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=True,
                     help="read the text on each frame and show it to the model (needs pip install 'specto[ocr]')")
    run.add_argument("--whisper-model", default="base", help="faster-whisper model size when transcribing locally")
    run.add_argument("--ingest-only", action="store_true", help="stop after frames and transcript")
    run.add_argument("--fake", action="store_true", help="use a fake model (no API key needed) to check the pipeline")
    run.add_argument("--force", action="store_true", help="redo every stage even if outputs exist")
    run.set_defaults(func=cmd_run)

    exp = sub.add_parser("export", help="rebuild the workbook and report from an output folder")
    exp.add_argument("out_dir")
    exp.set_defaults(func=cmd_export)

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

"""Command line entry point.

    specto run walkthrough.mp4 --out out/walkthrough
    specto run walkthrough.mp4 --transcript walkthrough.vtt --out out/walkthrough
    specto export out/walkthrough        # rebuild the workbook from analysis.json
    specto score out/walkthrough expected.json   # compare with an answer key
    specto doctor                        # what is installed, what is missing
    specto init                          # write a commented starter specto.toml for the team
    specto merge out/day1 out/day2 --out out/all   # several sessions, one workbook
    specto compare out/before out/after --out out/diff   # what changed between two analyses
    specto watch shared/incoming --out shared/out  # process every recording dropped into a folder
    specto requests out/walkthrough      # write the model requests as files (no key needed)
    specto load out/walkthrough          # read the answers back and write every output
    specto redact out/walkthrough        # paint over personal data on the frames
    specto answers out/walkthrough       # read the answers typed into the workbook
    specto resolve out/walkthrough       # answered questions become requirements
    specto live --out out/call           # during a call: screen + mic, questions every 5 minutes
    specto demo --open                   # a real example end to end, no key, no model call

Each stage saves its result in the output folder, so running the same command
again picks up where it left off. Pass --force to redo everything.

A specto.toml in the current folder (or --config PATH) sets flags once for the
team; a flag typed on the command line always beats it. Run specto init.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as project_config
from .config import UNSET
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


def run_pipeline(source: Path, out_dir: Path, args: argparse.Namespace) -> int:
    """The single-item pipeline `specto run` uses: ingest, estimate (with the
    --max-cost cap), extract, export. `source` is a video file or a folder of
    screenshots; `out_dir` is where every stage writes its output. Shared by
    `cmd_run` and `cmd_watch` so the two never drift apart."""
    from .export import export_all
    from .extract import extract
    from .ingest import ingest

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


def cmd_run(args: argparse.Namespace) -> int:
    source = Path(args.video)
    out_dir = Path(args.out or Path("out") / (source.name or "run"))
    return run_pipeline(source, out_dir, args)


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

    if args.xlsx and args.from_html:
        print("answers: pass either --xlsx or --from-html, not both")
        return 2

    out_dir = Path(args.out_dir)
    result = import_answers(out_dir, args.from_html or args.xlsx)
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


def cmd_compare(args: argparse.Namespace) -> int:
    from .compare import compare_dirs

    dir_a, dir_b = Path(args.dir_a), Path(args.dir_b)
    out_dir = Path(args.out) if args.out else dir_a.parent / f"{dir_a.name}-vs-{dir_b.name}"
    comparison, md_path, html_path = compare_dirs(dir_a, dir_b, out_dir, label_a=args.label_a, label_b=args.label_b)
    print(f"compare: {len(comparison.added)} added, {len(comparison.removed)} removed, "
          f"{len(comparison.changed)} changed")
    print(f"wrote markdown: {md_path}")
    print(f"wrote html: {html_path}")
    return 0


WATCH_VIDEO_SUFFIXES = (".mp4", ".mov", ".mkv", ".webm")
WATCH_TRANSCRIPT_SUFFIXES = (".vtt", ".srt", ".json", ".txt", ".md")
WATCH_STATE_FILENAME = ".specto-watch-state.json"


def _watch_items(folder: Path, out_root: Path) -> list[tuple[str, str, Path]]:
    """New-item candidates in `folder`: `(stem, kind, source)` for every video
    file and every subfolder that looks like a screenshot set (holds at least
    one image), skipping dotfiles and the output folder itself."""
    from .live import SCREENSHOT_SUFFIXES

    try:
        out_root_resolved = out_root.resolve()
    except OSError:
        out_root_resolved = out_root
    items = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name):
        if entry.name.startswith("."):
            continue
        if entry.resolve() == out_root_resolved:
            continue
        if entry.is_file() and entry.suffix.lower() in WATCH_VIDEO_SUFFIXES:
            items.append((entry.stem, "video", entry))
        elif entry.is_dir():
            if any(p.is_file() and p.suffix.lower() in SCREENSHOT_SUFFIXES for p in entry.iterdir()):
                items.append((entry.name, "screenshots", entry))
    return items


def _watch_sibling_transcript(folder: Path, stem: str) -> Optional[Path]:
    """A transcript or notes file dropped next to the item, `stem.vtt` etc.
    Watch mode has no per-item --transcript flag, so this is the only way an
    item picks one up; without one, video is transcribed locally and a
    screenshot set is read with no notes."""
    for suffix in WATCH_TRANSCRIPT_SUFFIXES:
        candidate = folder / f"{stem}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _watch_source_mtime(kind: str, source: Path) -> float:
    """The newest mtime among a video file, or the files inside a screenshot
    folder, used to tell a touched-up source from one already processed."""
    if kind == "video":
        return source.stat().st_mtime
    mtimes = [p.stat().st_mtime for p in source.iterdir() if p.is_file()]
    return max(mtimes) if mtimes else source.stat().st_mtime


def _watch_item_args(args: argparse.Namespace) -> argparse.Namespace:
    """The run_pipeline options for one watched item: the settings watch was
    given (flags, then specto.toml, then the built-in defaults), the same ones
    `specto run` would use, plus the per-item plumbing."""
    v = lambda name: project_config.value_or_default(args, name)  # noqa: E731
    return argparse.Namespace(
        model=v("model"), provider=v("provider"), effort=v("effort"), reader_model=v("reader_model"),
        frames_per_call=v("frames_per_call"), max_frames=v("max_frames"), crop=v("crop"), detect=v("detect"),
        hash_distance=v("hash_distance"), sample_fps=v("sample_fps"), scene_threshold=0.3, ocr=v("ocr"),
        whisper_model=v("whisper_model"), ingest_only=False, estimate=False, max_cost=v("max_cost"),
        fake=args.fake, force=False, transcript=None,
    )


def _watch_pass(folder: Path, out_root: Path, args: argparse.Namespace, state: dict) -> tuple[int, int, int]:
    """One pass over `folder`: process every new or touched item, skip the
    rest, and return (processed, skipped, failed). Updates `state` in place;
    the caller writes it to disk."""
    processed = skipped = failed = 0
    for stem, kind, source in _watch_items(folder, out_root):
        item_out = out_root / stem
        analysis_path = item_out / "analysis.json"
        src_mtime = _watch_source_mtime(kind, source)
        reprocess = False
        if analysis_path.exists():
            if src_mtime > analysis_path.stat().st_mtime:
                reprocess = True
            else:
                skipped += 1
                continue

        print(f"watch: {'reprocessing' if reprocess else 'processing'} {stem} ({kind})")
        item_args = _watch_item_args(args)
        transcript = _watch_sibling_transcript(folder, stem)
        item_args.transcript = str(transcript) if transcript else None
        item_args.force = reprocess
        try:
            rc = run_pipeline(source, item_out, item_args)
            if rc != 0:
                raise RuntimeError(f"run_pipeline exited with code {rc}")
        except Exception as error:
            failed += 1
            print(f"watch: FAILED {stem}: {error}", file=sys.stderr)
            state[stem] = {
                "kind": kind, "source": str(source), "source_mtime": src_mtime,
                "out_dir": str(item_out), "status": "error", "error": str(error),
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            processed += 1
            print(f"watch: done {stem} -> {item_out}")
            state[stem] = {
                "kind": kind, "source": str(source), "source_mtime": src_mtime,
                "out_dir": str(item_out), "status": "ok", "error": None,
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
    return processed, skipped, failed


def cmd_watch(args: argparse.Namespace) -> int:
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"watch: {folder} is not a folder", file=sys.stderr)
        return 2
    out_root = Path(args.out) if args.out else folder.parent / "out"
    out_root.mkdir(parents=True, exist_ok=True)
    state_path = folder / WATCH_STATE_FILENAME
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    totals = {"processed": 0, "skipped": 0, "failed": 0}

    def run_once() -> None:
        processed, skipped, failed = _watch_pass(folder, out_root, args, state)
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        totals["processed"] += processed
        totals["skipped"] += skipped
        totals["failed"] += failed
        print(f"watch: pass done: {processed} processed, {skipped} skipped, {failed} failed "
              f"(running totals: {totals['processed']} processed, {totals['skipped']} skipped, "
              f"{totals['failed']} failed)")

    if args.once:
        run_once()
        return 0

    print(f"watch: watching {folder} every {args.interval:g}s, writing to {out_root} (Ctrl-C to stop)")
    try:
        while True:
            run_once()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("watch: stopped")
    return 0


DEMO_EXAMPLES = {
    "onboarding": {"crop": None},
    "claims": {"crop": "auto"},
}


def examples_root() -> Path:
    """The examples/ folder: $SPECTO_EXAMPLES, next to the package (a source
    checkout or an editable install), or under the current folder."""
    if os.environ.get("SPECTO_EXAMPLES"):
        return Path(os.environ["SPECTO_EXAMPLES"]).expanduser()
    beside = Path(__file__).resolve().parent.parent / "examples"
    if beside.is_dir():
        return beside
    return Path("examples")


def cmd_demo(args: argparse.Namespace) -> int:
    import re
    import shutil

    from .byo import ByoError, dump_requests, load_responses, requests_dir
    from .export import export_all
    from .extract import split_chunks
    from .ingest import ingest

    example = examples_root() / args.example
    reference = example / "reference"
    video, transcript = example / "walkthrough.mp4", example / "walkthrough.vtt"
    missing = [p for p in (video, transcript, reference) if not p.exists()]
    if missing:
        print(f"demo: cannot find {', '.join(str(p) for p in missing)}. The demo needs the examples folder from "
              f"the specto repository; run it from a checkout or set SPECTO_EXAMPLES.", file=sys.stderr)
        return 2
    out_dir = Path(args.out or Path("out") / f"demo-{args.example}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"demo: the {args.example} example, answered with a saved real reading; no key, no model call")

    recording = ingest(video, out_dir, transcript_path=transcript, whisper_model=None,
                       crop=DEMO_EXAMPLES[args.example]["crop"])
    print(f"ingest: {len(recording.keyframes)} frames, {len(recording.segments)} transcript segments, "
          f"{recording.duration:.0f}s of video")
    ocr_text = None
    from .ocr import ocr_available
    if ocr_available():
        from .ocr import ocr_recording
        ocr_text = ocr_recording(recording, out_dir) or None

    frames_per_call = 8
    chunk_count = len(split_chunks(recording, frames_per_call))
    answers = sorted(reference.glob("*.response.json"))
    reference_chunks = [p for p in answers if re.fullmatch(r"chunk_\d+\.response\.json", p.name)]
    if chunk_count != len(reference_chunks):
        print(f"demo: this build of the recording splits into {chunk_count} chunk(s), but the saved reference "
              f"answers cover {len(reference_chunks)}. The reference was made from a different build of the "
              f"recording, so its answers would not line up with these frames. Rebuild the reference, or "
              f"check out the recording it was made from.", file=sys.stderr)
        return 2

    folder = requests_dir(out_dir)
    if folder.exists():
        shutil.rmtree(folder)
    try:
        dump_requests(recording, out_dir, frames_per_call=frames_per_call, ocr_text=ocr_text)
        for path in answers:
            shutil.copyfile(path, folder / path.name)
        print(f"demo: copied {len(answers)} reference answer file(s) into {folder}")
        analysis = load_responses(recording, out_dir)
    except ByoError as error:
        print(f"demo: {error}", file=sys.stderr)
        return 2
    print(f"loaded: {len(analysis.screens)} screens, {len(analysis.fields)} fields, "
          f"{len(analysis.requirements)} requirements, {len(analysis.acceptance_criteria)} criteria, "
          f"{len(analysis.questions)} questions")
    paths = export_all(analysis, recording, out_dir)
    for name, p in paths.items():
        print(f"wrote {name}: {p}")
    html = paths["html"].resolve()
    print(f"\nOpen the report:   {html}")
    print(f"Open the workbook: {paths['xlsx'].resolve()}")
    if args.open:
        import webbrowser

        webbrowser.open(html.as_uri())
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import Check, main as doctor_main

    try:
        config = project_config.load_config(getattr(args, "config", None))
    except project_config.ConfigError as error:
        check = Check(name="Project settings", ok=False, detail=str(error))
    else:
        if config is None:
            check = Check(name="Project settings", ok=False,
                          detail=f"no {project_config.CONFIG_FILENAME} here (optional; specto init writes a commented starter)")
        else:
            check = Check(name="Project settings", ok=True, detail=project_config.describe(config))
    return doctor_main([], extra_checks=[check])


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(project_config.CONFIG_FILENAME)
    if path.exists() and not args.force:
        print(f"init: {path} already exists; pass --force to replace it", file=sys.stderr)
        return 2
    path.write_text(project_config.starter_text(), encoding="utf-8")
    print(f"init: wrote {path}. Every setting is commented out; remove the # from a line to use it.")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from .score import main as score_main

    return score_main([args.out_dir, args.expected, "--min", str(args.min)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="specto", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    cfg = argparse.ArgumentParser(add_help=False)
    cfg.add_argument("--config", metavar="PATH",
                     help="project settings file (default: ./specto.toml if there is one); a flag typed here beats the file")

    run = sub.add_parser("run", parents=[cfg], help="watch a recording and write the workbook")
    run.add_argument("video", help="video file (mp4, mov, mkv, webm), or a folder of screenshots (png, jpg, webp)")
    run.add_argument("--transcript", help="transcript file (.vtt, .srt, .txt, .json); transcribed locally if omitted. "
                     "With a screenshot folder, plain notes (.txt or .md without timestamps) are spread over the screenshots")
    run.add_argument("--out", default=UNSET, help="output folder (default: out/<video name>)")
    run.add_argument("--model", default=UNSET, help="Claude model id (default: claude-opus-5)")
    run.add_argument("--provider", default=UNSET, choices=["claude", "gemini"], help="which model service to call (default claude)")
    run.add_argument("--effort", default=UNSET, choices=["low", "medium", "high", "xhigh", "max"])
    run.add_argument("--reader-model", default=UNSET, help="a cheaper model for reading the frames, e.g. claude-sonnet-5; the merge still uses --model")
    run.add_argument("--frames-per-call", type=int, default=UNSET, help="frames sent per model call (default 8)")
    run.add_argument("--max-frames", type=int, default=UNSET, help="cap on still frames kept; the least-changed frames go first (default 240)")
    run.add_argument("--crop", metavar="x,y,w,h|auto", default=UNSET, help="keep only this part of the picture (pixels), or 'auto' to find the shared window and drop the border, toolbar and gallery strip around it")
    run.add_argument("--detect", default=UNSET, choices=["hash", "scene"],
                     help="how screen changes are found: image hash (default) or ffmpeg brightness")
    run.add_argument("--hash-distance", type=int, default=UNSET, help="how different a frame must be to count as new (default 8; lower catches typed text)")
    run.add_argument("--sample-fps", type=float, default=UNSET, help="frames looked at per second in hash mode (default 1)")
    run.add_argument("--scene-threshold", type=float, default=0.3, help="ffmpeg scene-change threshold 0..1, scene mode only (default 0.3)")
    run.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=UNSET,
                     help="read the text on each frame and show it to the model (needs pip install 'specto[ocr]')")
    run.add_argument("--whisper-model", default=UNSET, help="faster-whisper model size when transcribing locally")
    run.add_argument("--ingest-only", action="store_true", help="stop after frames and transcript")
    run.add_argument("--estimate", action="store_true", help="print the expected cost for each model and stop before calling one")
    run.add_argument("--max-cost", type=float, metavar="USD", default=UNSET, help="stop before the model if the estimate is above this many dollars")
    run.add_argument("--fake", action="store_true", help="use a fake model (no API key needed) to check the pipeline")
    run.add_argument("--force", action="store_true", help="redo every stage even if outputs exist")
    run.set_defaults(func=cmd_run)

    exp = sub.add_parser("export", help="rebuild the workbook and report from an output folder")
    exp.add_argument("out_dir")
    exp.set_defaults(func=cmd_export)

    live = sub.add_parser("live", parents=[cfg], help="watch the shared screen and microphone during a call (macOS)")
    live.add_argument("--out", default=UNSET, help="output folder; an existing one is resumed (required, here or in specto.toml)")
    live.add_argument("--replay", metavar="DIR", help="instead of capturing, feed a folder of screenshots named shot_<seconds>.png")
    live.add_argument("--transcript", help="transcript file to use with --replay")
    live.add_argument("--every", type=float, default=300, help="seconds between analyses (default 300)")
    live.add_argument("--interval", type=float, default=3.0, help="seconds between screenshots (default 3)")
    live.add_argument("--audio-chunk", type=float, default=30, help="seconds of microphone per transcribed chunk (default 30)")
    live.add_argument("--display", type=int, default=1, help="which display to capture (default 1)")
    live.add_argument("--audio-device", default=":0", help="ffmpeg avfoundation audio device (default :0)")
    live.add_argument("--whisper-model", default=UNSET)
    live.add_argument("--model", default=UNSET)
    live.add_argument("--provider", default=UNSET, choices=["claude", "gemini"], help="which model service to call (default claude)")
    live.add_argument("--effort", default=UNSET, choices=["low", "medium", "high", "xhigh", "max"])
    live.add_argument("--fake", action="store_true", help="stand-in model, no key needed")
    live.set_defaults(func=cmd_live)

    an = sub.add_parser("answers", help="read the Answer and Status typed into the workbook, or exported from report.html, back into the analysis")
    an.add_argument("out_dir")
    an.add_argument("--xlsx", help="the workbook with the answers (default: out_dir/analysis.xlsx)")
    an.add_argument("--from-html", help="the answers.json exported from report.html's Export answers button")
    an.set_defaults(func=cmd_answers)

    rs = sub.add_parser("resolve", parents=[cfg], help="turn answered questions into requirements and criteria")
    rs.add_argument("out_dir")
    rs.add_argument("--model", default=UNSET)
    rs.add_argument("--provider", default=UNSET, choices=["claude", "gemini"], help="which model service to call (default claude)")
    rs.add_argument("--effort", default=UNSET, choices=["low", "medium", "high", "xhigh", "max"])
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

    cp = sub.add_parser("compare", help="compare two finished analyses of the same journey: what changed")
    cp.add_argument("dir_a", metavar="DIR_A", help="output folder of the first (e.g. 'before') run")
    cp.add_argument("dir_b", metavar="DIR_B", help="output folder of the second (e.g. 'after') run")
    cp.add_argument("--out", help="folder for compare.md and compare.html (default: a new folder next to DIR_A)")
    cp.add_argument("--label-a", default="before", help="label for DIR_A in the report (default: before)")
    cp.add_argument("--label-b", default="after", help="label for DIR_B in the report (default: after)")
    cp.set_defaults(func=cmd_compare)

    wt = sub.add_parser("watch", parents=[cfg], help="watch a shared folder and process every recording dropped into it, for a team")
    wt.add_argument("folder", help="folder to watch for new video files and new screenshot-set subfolders")
    wt.add_argument("--out", default=UNSET, help="where each item's output goes, one subfolder per item (default: FOLDER/../out)")
    wt.add_argument("--model", default=UNSET, help="Claude model id (default: claude-opus-5)")
    wt.add_argument("--provider", default=UNSET, choices=["claude", "gemini"], help="which model service to call (default claude)")
    wt.add_argument("--fake", action="store_true", help="use a fake model (no API key needed) to check the pipeline")
    wt.add_argument("--max-cost", type=float, metavar="N", default=UNSET, help="skip an item's model call if its estimate is above this many dollars")
    wt.add_argument("--interval", type=float, default=UNSET, help="seconds between polls of the folder (default 10)")
    wt.add_argument("--once", action="store_true", help="process everything currently in the folder and exit, instead of looping")
    wt.set_defaults(func=cmd_watch)

    dm = sub.add_parser("demo", parents=[cfg], help="run a real example end to end with a saved reading: no key, no model call")
    dm.add_argument("--example", default="onboarding", choices=sorted(DEMO_EXAMPLES), help="which example (default onboarding)")
    dm.add_argument("--out", default=UNSET, help="output folder (default: out/demo-<example>)")
    dm.add_argument("--open", action="store_true", help="open report.html in the default browser when done")
    dm.set_defaults(func=cmd_demo)

    ini = sub.add_parser("init", help="write a commented starter specto.toml in this folder")
    ini.add_argument("--force", action="store_true", help="replace specto.toml if it already exists")
    ini.set_defaults(func=cmd_init)

    doc = sub.add_parser("doctor", parents=[cfg], help="check what is installed and what is missing")
    doc.set_defaults(func=cmd_doctor)

    sc = sub.add_parser("score", help="compare an output folder with a hand-written answer key")
    sc.add_argument("out_dir")
    sc.add_argument("expected", help="expected.json answer key")
    sc.add_argument("--min", type=float, default=0.0, help="exit 1 if overall recall is below this")
    sc.set_defaults(func=cmd_score)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in project_config.COMMAND_KEYS:
        try:
            config = project_config.load_config(args.config)
        except project_config.ConfigError as error:
            print(error, file=sys.stderr)
            return 2
        from_file = project_config.apply_config(args, config)
        if from_file:
            print(f"config: {config.path} sets {', '.join(from_file)}")
        if args.command == "live" and not args.out:
            parser.error("the following arguments are required: --out (or set out in specto.toml)")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

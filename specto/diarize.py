"""Label who said what, from the audio, when several people are on the call.

`assign_speakers` fills in `TranscriptSegment.speaker` for segments a
transcript did not already name (a `<v Name>` VTT tag or a meeting export
already carries a name; a transcript specto made itself with `transcribe()`
never does, since faster-whisper only recognises words, not voices).

This is an optional extra, `pip install "specto[speakers]"`, because it pulls
in a new dependency, sherpa-onnx. Unlike pyannote's own diarization pipeline,
which needs a Hugging Face account and a gated model (`PLAN.md`'s original
Phase 4 note), sherpa-onnx runs the same segmentation model (pyannote's
`segmentation-3.0`, re-exported to ONNX) plus a speaker-embedding model, both
downloaded straight from the sherpa-onnx project's GitHub releases: no
account, no token, nothing to accept. `ensure_models` fetches the two files
(about 33 MB total) to a local cache the first time it runs; later calls
reuse them.

**Measured, not assumed.** On a synthetic three-speaker test clip built for
this feature (three distinct macOS `say` voices, six turns, half-second gaps;
`tests/fixtures/diarize/clip.wav`, ground truth in the same folder's
`ground_truth.json`, exercised by `tests/test_diarize.py`), every one of the
6 turns clustered onto the right speaker and all 5 speaker changes landed
within 0.1s of the true boundary: 6/6 turns, 5/5 changes, 0 false changes.
That clip has three clearly different voices and clean silence between
turns, which is an easy case; two people who sound alike, cross-talk, or a
noisy recording will all do worse. Treat the above as one honest data point,
not a general accuracy rate for real calls.
"""
from __future__ import annotations

import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Optional

from specto.model import TranscriptSegment

SEGMENTATION_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
EMBEDDING_URL = (
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
    "speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
)
SEGMENTATION_MEMBER = "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
EMBEDDING_FILENAME = "3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx"
DIARIZE_SAMPLE_RATE = 16000

DEFAULT_MODEL_DIR = Path.home() / ".cache" / "specto" / "diarize-models"
DEFAULT_THRESHOLD = 0.5  # sherpa-onnx's own example default; lower finds more speakers


def models_cached(model_dir: Path | str = DEFAULT_MODEL_DIR) -> bool:
    """True when both models are already downloaded to `model_dir`."""
    model_dir = Path(model_dir)
    return (model_dir / SEGMENTATION_MEMBER).exists() and (model_dir / EMBEDDING_FILENAME).exists()


def ensure_models(model_dir: Path | str = DEFAULT_MODEL_DIR) -> tuple[Path, Path]:
    """Download the segmentation and embedding models once, and return their paths.

    Both come straight from https://github.com/k2-fsa/sherpa-onnx/releases:
    no Hugging Face account, no token, nothing to accept. Safe to call every
    time; it only downloads what is missing.
    """
    model_dir = Path(model_dir)
    seg_path = model_dir / SEGMENTATION_MEMBER
    emb_path = model_dir / EMBEDDING_FILENAME

    if not seg_path.exists():
        archive = model_dir / "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
        _download(SEGMENTATION_URL, archive)
        with tarfile.open(archive) as tar:
            tar.extract(tar.getmember(SEGMENTATION_MEMBER), model_dir)
        archive.unlink(missing_ok=True)

    if not emb_path.exists():
        _download(EMBEDDING_URL, emb_path)

    return seg_path, emb_path


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"diarize: downloading {url.rsplit('/', 1)[-1]} (one-off, cached under {dest.parent})")
    tmp = dest.with_name(dest.name + ".part")
    urllib.request.urlretrieve(url, tmp)
    tmp.rename(dest)


def _extract_audio(source: str | Path, sample_rate: int = DIARIZE_SAMPLE_RATE):
    """Mono float32 PCM samples at `sample_rate`, decoded from a video or audio
    file with the same bundled ffmpeg `specto.ingest` uses (no system ffmpeg
    needed)."""
    import numpy as np
    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-i", str(source),
           "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not result.stdout:
        stderr = result.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"diarize: ffmpeg could not read audio from {source}: {stderr}")
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    return samples


def diarize_turns(
    audio_source: str | Path,
    *,
    model_dir: Path | str = DEFAULT_MODEL_DIR,
    num_speakers: Optional[int] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[tuple[float, float, int]]:
    """Run sherpa-onnx diarization and return `(start, end, speaker_index)`
    turns in time order, `speaker_index` starting at 0.

    `num_speakers` fixes the cluster count when it is known; left as None,
    `threshold` decides it (lower finds more speakers). Raises ImportError
    with an install hint when sherpa-onnx is not installed.
    """
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise ImportError(
            "sherpa-onnx is not installed, so specto cannot label speakers from the audio. "
            'pip install "specto[speakers]".'
        ) from exc

    seg_model, emb_model = ensure_models(model_dir)
    samples = _extract_audio(audio_source)

    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(seg_model)),
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(emb_model)),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=num_speakers or -1, threshold=threshold),
        min_duration_on=0.2,
        min_duration_off=0.4,
    )
    if not config.validate():
        raise RuntimeError("diarize: sherpa-onnx rejected its own config (a model path is likely wrong)")

    diarization = sherpa_onnx.OfflineSpeakerDiarization(config)
    if diarization.sample_rate != DIARIZE_SAMPLE_RATE:
        raise RuntimeError(
            f"diarize: model expects {diarization.sample_rate} Hz audio, extracted {DIARIZE_SAMPLE_RATE} Hz"
        )
    result = diarization.process(samples).sort_by_start_time()
    turns = [(r.start, r.end, int(r.speaker)) for r in result]
    speakers = {t[2] for t in turns}
    print(f"diarize: {len(turns)} speaker turns found across {len(speakers)} speaker(s)")
    return turns


def label_segments(
    segments: list[TranscriptSegment], turns: list[tuple[float, float, int]]
) -> list[TranscriptSegment]:
    """Set `.speaker` on every segment that does not already have one, from
    whichever diarized turn overlaps it the most. A segment with no overlap
    at all (silence, or diarization found nothing there) is left unchanged.
    Pure Python, no model: kept separate from `diarize_turns` so the label
    logic can be tested without sherpa-onnx installed.
    """
    labelled = []
    for segment in segments:
        if segment.speaker or not turns:
            labelled.append(segment)
            continue
        speaker = _dominant_speaker(segment, turns)
        if speaker is None:
            labelled.append(segment)
        else:
            labelled.append(segment.model_copy(update={"speaker": f"Speaker {speaker + 1}"}))
    return labelled


def _dominant_speaker(segment: TranscriptSegment, turns: list[tuple[float, float, int]]) -> Optional[int]:
    best_speaker: Optional[int] = None
    best_overlap = 0.0
    for start, end, speaker in turns:
        overlap = min(segment.end, end) - max(segment.start, start)
        if overlap > best_overlap:
            best_overlap, best_speaker = overlap, speaker
    return best_speaker


def assign_speakers(
    segments: list[TranscriptSegment],
    audio_source: str | Path,
    *,
    model_dir: Path | str = DEFAULT_MODEL_DIR,
    num_speakers: Optional[int] = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[TranscriptSegment]:
    """Label each segment with a speaker, from the recording's own audio.

    Segments that already carry a speaker name (a `<v Name>` VTT tag or a
    meeting export) are left exactly as they are; diarization only fills in
    the gaps a transcript format did not already name. See the module
    docstring for what was measured and what was not.
    """
    if not segments:
        return segments
    turns = diarize_turns(audio_source, model_dir=model_dir, num_speakers=num_speakers, threshold=threshold)
    return label_segments(segments, turns)

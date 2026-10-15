"""Stem separation, run as a separate process so a job can be cancelled.

    python stems.py INPUT.wav OUTPUT_DIR MODEL_DIR

Step 1 splits the song into vocals, drums, bass and other (Demucs).
Step 2 splits the drums into kick, snare, toms, hihat, ride and crash (DrumSep).
Progress lines ("STEP 1", "STEP 2", "DONE") are printed to stdout.
"""
import os
import re
import shutil
import sys
from pathlib import Path

from audio_separator.separator import Separator

SONG_MODEL = os.environ.get("VIDTOMP4_STEMS_MODEL", "htdemucs.yaml")
DRUM_MODEL = os.environ.get("VIDTOMP4_DRUMS_MODEL", "MDX23C-DrumSep-aufr33-jarredou.ckpt")
RENAME = {"hh": "hihat"}


def separate(model: str, source: Path, dest: Path, model_dir: str) -> dict[str, Path]:
    """Run one model and move its outputs to dest/<stem>.wav. Returns {stem: path}."""
    work = dest / ".work"
    work.mkdir(parents=True, exist_ok=True)
    sep = Separator(model_file_dir=model_dir, output_dir=str(work), output_format="WAV")
    sep.load_model(model)
    stems = {}
    for name in sep.separate(str(source)):
        m = re.search(r"_\(([^)]+)\)_", Path(name).name)
        stem = m.group(1).lower() if m else Path(name).stem
        stem = RENAME.get(stem, stem)
        target = dest / f"{stem}.wav"
        shutil.move(str(work / Path(name).name), target)
        stems[stem] = target
    shutil.rmtree(work, ignore_errors=True)
    return stems


def main(source: str, out: str, model_dir: str) -> None:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    print("STEP 1", flush=True)
    stems = separate(SONG_MODEL, Path(source), out_dir, model_dir)
    if "drums" not in stems:
        sys.exit(f"The song model produced no drums stem (got: {', '.join(stems)}).")
    print("STEP 2", flush=True)
    separate(DRUM_MODEL, stems["drums"], out_dir / "drums", model_dir)
    print("DONE", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:4])

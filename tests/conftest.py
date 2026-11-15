"""Shared helpers. Every test makes its own small clips with ffmpeg; nothing of the user's is read or written."""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import app  # noqa: E402
import grade  # noqa: E402

RATE = 48000
BOTH = "pan=stereo|c0=c0|c1=c0"  # a mono test sound at full level in both channels (ffmpeg's own upmix lowers it by 3 dB)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *map(str, args)], check=True)


def make_video(path, size="320x180", fps=30, seconds=3, tone=None, picture="color=c=gray", turn=None, mono=False):
    """An HEVC clip of a plain picture, with a sine tone (or silence) as its sound."""
    sound = f"sine=frequency={tone}:sample_rate={RATE}" if tone else f"anullsrc=r={RATE}:cl=stereo"
    flat = path.with_suffix(".flat.mp4") if turn else path
    ffmpeg("-f", "lavfi", "-i", f"{picture}{':' if '=' in picture else '='}s={size}:r={fps}", "-f", "lavfi", "-i", sound, "-t", seconds,
           *([] if mono else ["-af", BOTH]), "-c:v", "hevc_videotoolbox", "-tag:v", "hvc1", "-pix_fmt", "p010le", "-c:a", "aac", flat)
    if turn:  # stored on its side, as a phone or a rotated action camera does
        ffmpeg("-display_rotation", turn, "-i", flat, "-c", "copy", path)
    return path


def make_sound(path, graph, mono=False):
    ffmpeg("-f", "lavfi", "-i", graph, *([] if mono else ["-af", BOTH]), "-ar", RATE, path)
    return path


def samples(path):
    """The left channel of a file's sound, as floats."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<f4")[::2]


def level(x, hz):
    """Amplitude of one tone in a stretch of sound."""
    spectrum = np.abs(np.fft.rfft(x)) * 2 / len(x)
    freq = np.fft.rfftfreq(len(x), 1 / RATE)
    return float(spectrum[(freq > hz - 5) & (freq < hz + 5)].max())


def onsets(x, floor=0.02):
    """Times (s) at which sound starts after silence."""
    loud = np.convolve(np.abs(x) > floor, np.ones(480), "same") > 100
    return (np.flatnonzero(np.diff(loud.astype(int)) == 1) / RATE).tolist()


def frame(path, t):
    """Mean RGB (0-255) of the frame shown at time t."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"trim=start={t},scale=16:16", "-frames:v", "1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 3).mean(0)


def info(path):
    """(width, height, frame rate, frames, seconds) of a video as it is displayed."""
    w, h, fps, _ = app.probe(str(path))
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-of", "csv=p=0",
                          "-show_entries", "stream=nb_read_frames:format=duration", str(path)],
                         capture_output=True, check=True).stdout.decode().split()
    return w, h, fps, int(out[0].strip(",")), float(out[1])


@pytest.fixture
def work(tmp_path, monkeypatch):
    """A scratch folder standing in for the app's clip and export folders."""
    monkeypatch.setattr(app, "CLIP_DIR", tmp_path / "clips")
    monkeypatch.setattr(app, "EXPORT_DIR", tmp_path / "out")
    (tmp_path / "clips").mkdir()
    app.export.update(state="idle", progress=0, text="")
    app.job.clear()
    return tmp_path


def export(work, clips, **more):
    """Run an export of clips given as (input, start, end), graded with a LUT that changes nothing."""
    grade.write_cube(work / "clips" / "00000000.cube", lambda rgb: rgb)
    app.run_export([{"id": "00000000", "input": str(i), "start": a, "end": b} for i, a, b in clips], "test", False, **more)
    assert app.export["state"] == "done", app.export["text"]
    return Path(app.export["text"])


@pytest.fixture(params=["gpu", "ffmpeg"])
def engine(request, monkeypatch):
    """Run a test once on each export engine."""
    if request.param == "ffmpeg":
        monkeypatch.setattr(app, "gpu_tool", lambda: None)
    elif not app.gpu_tool():
        pytest.skip("the GPU exporter could not be built (no Swift compiler)")
    return request.param

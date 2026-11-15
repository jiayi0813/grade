#!/usr/bin/env python3
"""Local web UI for grade.py.

  .venv/bin/python app.py     then open http://127.0.0.1:8791
"""
import io
import json
import mimetypes
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image

import grade

PORT = 8791
ROOT = Path(__file__).parent
# LUT folder of the Mac App Store edition of DaVinci Resolve (free)
LUT_DIR = (Path.home() / "Library/Containers/com.blackmagic-design.DaVinciResolveLite"
           / "Data/Library/Application Support/LUT/Grade")
YT_FORMAT = "bv*[height<=1080][vcodec^=avc1]/bv*[height<=1080]/b"
CLIP_DIR = ROOT / "clips"  # one baked LUT and thumbnail per timeline clip
EXPORT_DIR = Path.home() / "Movies" / "Grade"
CLIP_ID = r"[0-9a-f]{8}"

frames = {}   # "source" / "reference" -> float RGB array
stats = {}    # same keys -> grade.stats() of that frame
streams = {}  # video URL -> (direct stream URL, duration)
playable = {}  # "source" -> the local video file the page may play, if there is one
export = {"state": "idle", "progress": 0, "text": ""}  # the one export job: idle / running / done / error
closeups = {}  # "before" / "after" -> PNG of the last close-up
job = {}      # "proc": the ffmpeg now running; "cancel": set once the user asks to stop


def run(cmd, timeout=120):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode:
        lines = r.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"{cmd[0]} failed")
    return r.stdout


def resolve_input(inp):
    """(ffmpeg input, duration in seconds) for a local path or a video page URL."""
    if re.match(r"https?://", inp):
        if inp not in streams:
            info = json.loads(run([sys.executable, "-m", "yt_dlp", "-j", "--no-playlist",
                                   "--js-runtimes", "node", "-f", YT_FORMAT, "--", inp]))
            streams[inp] = (info["url"], info.get("duration") or 0)
            for _ in range(5):  # YouTube refuses a newly issued stream link for a few seconds
                if not subprocess.run(["ffprobe", "-v", "error", info["url"]], capture_output=True).returncode:
                    break
                time.sleep(3)
        return streams[inp]
    path = Path(inp).expanduser()
    if not path.is_file():
        raise RuntimeError(f"File not found: {path}")
    fmt, _, dur = run(["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration",
                       "-of", "csv=p=0", str(path)]).decode().strip().rpartition(",")
    if fmt == "image2" or fmt.endswith("_pipe"):  # still image
        return str(path), 0
    return str(path), float(dur)


def extract(src, t, seek=True, width=960):
    """One frame as a float RGB array. Stills cannot be seeked."""
    # Apple hardware decoding makes 4K HEVC seeks several times faster
    video = ["-hwaccel", "videotoolbox", "-ss", str(t)] if seek else []
    png = run(["ffmpeg", "-v", "error", *video, "-i", src, "-frames:v", "1",
               "-vf", f"scale='min({width},iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"])
    if not png:
        raise RuntimeError("No frame at that time")
    return np.asarray(Image.open(io.BytesIO(png)).convert("RGB"), dtype=np.float64) / 255


def sample_times(t, duration, sample):
    """Extra moments whose frames feed the match statistics, besides the chosen frame."""
    if sample == "nearby":
        return [t + d for d in (-5, -2.5, 2.5, 5) if 0 <= t + d < duration]
    if sample == "whole":
        return [(i + 0.5) / 8 * duration for i in range(8)]
    return []


def grab(kind, inp, t, input_lut="", sample="frame"):
    src, duration = resolve_input(inp)
    if input_lut and not Path(input_lut).expanduser().is_file():
        raise RuntimeError(f"Input LUT not found: {input_lut}")

    for attempt in range(5):
        try:
            shots = [extract(src, t, seek=bool(duration))]
            break
        except RuntimeError:
            if inp not in streams:
                raise
            if attempt == 4:
                del streams[inp]  # link may have expired; the next load fetches a new one
                raise
            time.sleep(3)  # YouTube refuses a newly issued stream link for a few seconds

    def extra(when):  # a sample that fails is skipped; the chosen frame is enough on its own
        try:
            return extract(src, when, width=480)
        except (RuntimeError, subprocess.TimeoutExpired):
            return None

    with ThreadPoolExecutor(4) as pool:
        shots += [s for s in pool.map(extra, sample_times(t, duration, sample)) if s is not None]
    if input_lut:  # e.g. the camera's log-to-Rec.709 LUT; our LUT then goes on a later node
        lut = grade.read_cube(Path(input_lut).expanduser())
        shots = [grade.apply_lut(s, lut) for s in shots]
    frames[kind] = shots[0]
    playable[kind] = src if duration and inp not in streams else None
    stats[kind] = grade.stats(np.concatenate([shots[0][::2, ::2].reshape(-1, 3)]
                                             + [s.reshape(-1, 3) for s in shots[1:]]))
    return duration, len(shots)


def transform(p):
    look = grade.look_fn(**p["look"])
    if "reference" in frames and "source" in frames and (p["tone"] > 0 or p["colour"] > 0):
        match = grade.match_fn(stats["source"], stats["reference"], p["tone"], p["colour"])
        return lambda rgb: look(match(rgb))
    return look


def jpeg(rgb):
    buf = io.BytesIO()
    Image.fromarray((rgb * 255 + 0.5).astype(np.uint8)).save(buf, "JPEG", quality=92)
    return buf.getvalue()


# ---------- timeline clips and export ----------

def write_grade(path, p, fn):
    """Write the grade `fn` as the one LUT a video render uses, with the input LUT folded in."""
    input_lut, size = p.get("input_lut", "").strip(), grade.LUT_SIZE
    if input_lut:  # one finer LUT instead of two chained ones: each LUT costs render time
        lut, graded, size = grade.read_cube(Path(input_lut).expanduser()), fn, 64  # the GPU exporter's limit
        fn = lambda rgb: graded(grade.apply_lut(rgb, lut))
    grade.write_cube(path, fn, size)


def bake(p):
    """Freeze the current grade as a timeline clip: one LUT (input LUT folded in) and a thumbnail."""
    cid = p.get("id") or uuid.uuid4().hex[:8]
    if not re.fullmatch(CLIP_ID, cid):
        raise RuntimeError("Bad clip id")
    if "source" not in frames:
        raise RuntimeError("Load a video first")
    fn = transform(p)
    thumb = grade.apply_lut(frames["source"][::4, ::4], grade.as_grid(grade.build_lut(fn)))
    CLIP_DIR.mkdir(exist_ok=True)
    write_grade(CLIP_DIR / f"{cid}.cube", p, fn)
    (CLIP_DIR / f"{cid}.jpg").write_bytes(jpeg(thumb))
    return cid


def texture(c, h):
    """A clip's denoise and grain strengths (0 to 1) and its grain size in pixels of an h-pixel-high picture."""
    denoise, grain = (min(1, max(0, float(c.get(k, 0)))) for k in ("denoise", "grain"))
    return denoise, grain, min(8, max(1, float(c.get("grain_size", 2)))) * h / 1080  # grain size is set for 1080p


def closeup(p):
    """One spot of one frame at full size, rendered by the GPU exporter with and without denoise."""
    tool, src = gpu_tool(), resolve_input(p["input"])[0]
    if not tool:
        raise RuntimeError("The close-up needs the GPU exporter (install the Xcode Command Line Tools)")
    w, h, fps, _ = probe(src)
    denoise, grain, size = texture(p, h)
    cw, ch = min(w, int(p["width"])), min(h, int(p["height"]))
    x, y = (round(min(max(float(p[k]) * full - part / 2, 0), full - part)) for k, full, part in (("x", w, cw), ("y", h, ch)))
    with tempfile.TemporaryDirectory() as tmp:
        write_grade(f"{tmp}/lut.cube", p, transform(p))

        failed = []

        def shot(name, denoise):  # three frames: the one shown, and the two after it that denoise compares it with
            try:
                render(name, denoise)
            except (RuntimeError, subprocess.TimeoutExpired) as e:
                failed.append(str(e))

        def render(name, denoise):
            run([tool, src, str(p["time"]), str(float(3 / Fraction(fps))), f"{tmp}/lut.cube", f"{tmp}/{name}.mov",
                 str(w), str(h), str(denoise), str(grain), str(size)])
            closeups[name] = run(["ffmpeg", "-v", "error", "-i", f"{tmp}/{name}.mov", "-frames:v", "1",
                                  "-vf", f"crop={cw}:{ch}:{x}:{y}", "-f", "image2pipe", "-c:v", "png", "-"])

        both = [threading.Thread(target=shot, args=a) for a in (("before", 0), ("after", denoise))]
        closeups.clear()
        for t in both:
            t.start()
        for t in both:
            t.join()
        if failed:
            raise RuntimeError(f"No close-up of this frame: {failed[0]}")
    return {"x": (x + cw / 2) / w, "y": (y + ch / 2) / h, "width": cw, "frame": [w, h]}


def probe(src):
    """(width, height, frame rate, has audio) of a video, with the size as it is displayed."""
    show = ["ffprobe", "-v", "error", "-of", "csv=p=0", "-show_entries"]
    w, h, fps, *turn = run([*show, "stream=width,height,r_frame_rate:stream_side_data=rotation",
                            "-select_streams", "v:0", src]).decode().split()[0].split(",")
    if turn and turn[0] and round(float(turn[0])) % 180:  # stored on its side, e.g. portrait phone video
        w, h = h, w
    return int(w), int(h), fps, bool(run([*show, "stream=index", "-select_streams", "a", src]).strip())


def gpu_tool():
    """Path of the compiled GPU exporter, building it when missing or stale; None if it cannot be built."""
    src, exe = ROOT / "lutexport.swift", ROOT / "lutexport"
    try:
        if not exe.exists() or exe.stat().st_mtime < src.stat().st_mtime:
            export["text"] = "Building the GPU exporter"
            run(["swiftc", "-O", "-swift-version", "5", str(src), "-o", str(exe)], timeout=600)
        return str(exe)
    except (RuntimeError, OSError, subprocess.TimeoutExpired):
        return None  # no Swift compiler: ffmpeg renders everything, several times slower


def encode(cmd, done, span, total):
    """Run one render step of the export, updating progress. It writes `span` of `total` seconds."""
    proc = job["proc"] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in proc.stdout:
        if line.startswith("out_time_us=") and line[12:].strip().isdigit():
            export["progress"] = (done + min(span, int(line[12:]) / 1e6)) / total
    err = proc.stderr.read().strip().splitlines()
    if proc.wait():
        raise RuntimeError(err[-1] if err else f"{Path(cmd[0]).name} failed")


def run_export(clips, name, hd):
    """Render each clip through its LUT to matching 10-bit HEVC files, then join them.

    A clip may also ask for denoise (before the LUT) and film grain (after it), each 0 to 1.
    """
    ffmpeg = ["ffmpeg", "-v", "error", "-y", "-nostats", "-progress", "pipe:1"]
    # every part gets the same time base, or parts made by different engines join with wrong timing
    hevc = ["-tag:v", "hvc1", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-video_track_timescale", "90000"]
    aac = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    try:
        tool = gpu_tool()
        w, h, fps, _ = probe(resolve_input(clips[0]["input"])[0])  # the first clip sets size and rate
        if hd and h > 1080:
            w, h = round(w * 1080 / h / 2) * 2, 1080
        total = sum(c["end"] - c["start"] for c in clips)
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        shots = []  # per clip: ffmpeg input, seconds, LUT file, frame rate, has audio
        for c in clips:
            if not re.fullmatch(CLIP_ID, c["id"]):
                raise RuntimeError("Bad clip id")
            src = resolve_input(c["input"])[0]
            shots.append((src, c["end"] - c["start"], f"{CLIP_DIR / c['id']}.cube", *probe(src)[2:]))
        with tempfile.TemporaryDirectory(dir=EXPORT_DIR, prefix=".rendering-") as tmp:

            def render(gpu):
                done = 0
                for i, (c, (src, span, lut, rate, sound)) in enumerate(zip(clips, shots)):
                    if job.get("cancel"):
                        raise RuntimeError("Cancelled")
                    export["text"] = f"Rendering clip {i + 1} of {len(clips)}"
                    cut = ["-ss", str(c["start"]), "-t", str(span), "-i", src]
                    denoise, grain, size = texture(c, h)
                    # second input: the clip's audio, or silence, as every part needs an audio track to join
                    audio = cut if sound else ["-f", "lavfi", "-t", str(span), "-i", "anullsrc=r=48000:cl=stereo"]
                    if gpu:
                        encode([tool, src, str(c["start"]), str(span), lut, f"{tmp}/video.mov", str(w), str(h),
                                str(denoise), str(grain), str(size)], done, span, total)
                        encode([*ffmpeg, "-i", f"{tmp}/video.mov", *audio, "-map", "0:v:0", "-map", "1:a:0",
                                "-c:v", "copy", *hevc, *aac, f"{tmp}/{i}.mp4"], done + span, 0, total)
                    else:
                        # stand-ins for the GPU exporter's denoise and grain, tuned to about the same strength
                        clean = f"hqdn3d={6 * denoise}:{4.5 * denoise}:{9 * denoise}:{6.75 * denoise}," if denoise else ""
                        grains = (f"[p];color=gray:s={round(w / size)}x{round(h / size)}:r={fps},format=gray,"
                                  f"noise=alls={round(35 * grain)}:allf=t,format=gray10le,scale={w}:{h}:flags=bilinear,"
                                  "format=gbrp10le[g];[p][g]blend=all_mode=softlight:shortest=1") if grain else ""
                        encode([*ffmpeg, "-hwaccel", "videotoolbox", *cut, *audio, "-map", "0:v:0", "-map", "1:a:0",
                                "-vf", f"{clean}scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1,fps={fps},"
                                f"format=gbrp10le,lut3d=file='{lut}',pad={w}:{h}:-1:-1{grains},"
                                "scale=out_color_matrix=bt709:out_range=tv,format=p010le",
                                "-c:v", "hevc_videotoolbox", "-q:v", "55", *hevc, *aac, f"{tmp}/{i}.mp4"],
                               done, span, total)
                    done += span

            # One engine renders the whole timeline: Apple's players cannot decode a file joined from
            # parts made by both. The GPU exporter does not change frame rate, so it needs every clip
            # at the timeline's rate; if it cannot read a clip (e.g. a web link), ffmpeg starts over.
            gpu = tool and all(abs(Fraction(shot[3]) - Fraction(fps)) < 0.01 for shot in shots)
            if gpu:
                try:
                    render(True)
                except RuntimeError as e:
                    if job.get("cancel"):
                        raise
                    print(f"GPU export failed, starting over with ffmpeg: {e}", flush=True)
                    gpu = False
            if not gpu:
                render(False)
            parts = [f"{tmp}/{i}.mp4" for i in range(len(clips))]
            if job.get("cancel"):
                raise RuntimeError("Cancelled")
            out, n = EXPORT_DIR / f"{name}.mp4", 1
            while out.exists():  # never overwrite an earlier export
                n += 1
                out = EXPORT_DIR / f"{name} {n}.mp4"
            if len(parts) == 1:
                Path(parts[0]).rename(out)
            else:
                export["text"] = "Joining clips"
                Path(tmp, "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
                run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", f"{tmp}/list.txt",
                     "-c", "copy", "-movflags", "+faststart", str(out)], timeout=None)
        export.update(state="done", progress=1, text=str(out))
    except Exception as e:
        export.update(state="error", text="Cancelled" if job.get("cancel") else str(e))


def pick_file(lut=False):
    choose = ('choose file with prompt "Choose a .cube LUT" default location POSIX file "%s"'
              % LUT_DIR.parent) if lut else 'choose file with prompt "Choose a video or image"'
    r = subprocess.run(["osascript", "-e", "tell me to activate", "-e", f"POSIX path of ({choose})"],
                       capture_output=True, timeout=600)
    return r.stdout.decode().strip()  # empty if cancelled


class Handler(BaseHTTPRequestHandler):
    def send(self, body, ctype="application/json", code=200):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_video(self, path):
        """A file with byte ranges, which browsers need to seek within a video."""
        size = Path(path).stat().st_size
        want = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(want[1]) if want else 0
        end = min(int(want[2]), size - 1) if want and want[2] else size - 1
        self.send_response(206 if want else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if want:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser drops a request whenever it seeks

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self.send((ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ("/img/source", "/img/reference") and path[5:] in frames:
            self.send(jpeg(frames[path[5:]]), "image/jpeg")
        elif re.fullmatch(rf"/clip/{CLIP_ID}\.jpg", path) and (CLIP_DIR / path[6:]).is_file():
            self.send((CLIP_DIR / path[6:]).read_bytes(), "image/jpeg")
        elif path == "/api/export":
            self.send(export)
        elif path in ("/closeup/before.png", "/closeup/after.png") and path[9:-4] in closeups:
            self.send(closeups[path[9:-4]], "image/png")
        elif path == "/video/source" and playable.get("source"):
            self.send_video(playable["source"])
        else:
            self.send({"error": "not found"}, code=404)

    def do_POST(self):
        # JSON-only: stops other websites from posting to this local server
        if self.headers.get("Content-Type") != "application/json":
            return self.send({"error": "json only"}, code=400)
        p = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        try:
            if self.path == "/api/pick":
                self.send({"path": pick_file(p.get("lut", False))})
            elif self.path == "/api/frame" and not p["input"].strip():  # cleared: stop matching to it
                frames.pop(p["kind"], None)
                stats.pop(p["kind"], None)
                self.send({})
            elif self.path == "/api/frame":
                duration, samples = grab(p["kind"], p["input"].strip(), float(p["time"]),
                                         p.get("input_lut", "").strip(), p.get("sample", "frame"))
                self.send({"duration": duration, "samples": samples})
            elif self.path == "/api/preview":
                lut = grade.as_grid(grade.build_lut(transform(p)))
                self.send(jpeg(grade.apply_lut(frames["source"], lut)), "image/jpeg")
            elif self.path == "/api/lut":  # the grade as raw float32 RGB rows, for the page's video shader
                self.send(grade.build_lut(transform(p)).astype("<f4").tobytes(), "application/octet-stream")
            elif self.path == "/api/cube":  # an input LUT file in the same form
                rows = grade.read_cube(Path(p["path"].strip()).expanduser()).transpose(2, 1, 0, 3)
                self.send(rows.astype("<f4").tobytes(), "application/octet-stream")
            elif self.path == "/api/save":
                name = re.sub(r"[^\w\- ]", "", p["name"]).strip() or "grade"
                LUT_DIR.mkdir(parents=True, exist_ok=True)
                grade.write_cube(LUT_DIR / f"{name}.cube", transform(p))
                self.send({"path": str(LUT_DIR / f"{name}.cube")})
            elif self.path == "/api/clip":
                self.send({"id": bake(p)})
            elif self.path == "/api/closeup":
                self.send(closeup(p))
            elif self.path == "/api/clip/remove":
                if re.fullmatch(CLIP_ID, p["id"]):
                    for ext in ("cube", "jpg"):
                        (CLIP_DIR / f"{p['id']}.{ext}").unlink(missing_ok=True)
                self.send({})
            elif self.path == "/api/export":
                if export["state"] == "running":
                    raise RuntimeError("An export is already running")
                name = re.sub(r"[^\w\- ]", "", p["name"]).strip() or "video"
                job.clear()
                export.update(state="running", progress=0, text="Starting")
                threading.Thread(target=run_export, args=(p["clips"], name, p.get("hd", False)),
                                 daemon=True).start()
                self.send(export)
            elif self.path == "/api/export/cancel":
                job["cancel"] = True
                if "proc" in job:
                    job["proc"].terminate()
                self.send({})
            else:
                self.send({"error": "not found"}, code=404)
        except Exception as e:  # show any failure in the UI instead of a dead request
            self.send({"error": str(e)}, code=500)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"Grade UI running at http://127.0.0.1:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()

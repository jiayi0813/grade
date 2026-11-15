# Grade — colour grade, cut and export

A local tool that colour-matches a video to a reference (a file or a YouTube link) and lets you
add a look on top. The result can leave the app two ways:

- **Export video**: cut graded clips, put them in order on a timeline and render one file.
  No DaVinci Resolve needed.
- **Save LUT**: write the grade as a `.cube` LUT into DaVinci Resolve's LUT folder.

To start, stop or troubleshoot the app, see [START_APP.md](START_APP.md).

## Install

Needs a Mac with Apple silicon. It was built and tested on one machine (M2 Max, macOS 26).

```bash
brew install ffmpeg node
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Then open http://127.0.0.1:8791. For fast export, install the Xcode Command Line Tools
(`xcode-select --install`); the GPU exporter is compiled from `lutexport.swift` on first use.

Not included: camera LUTs (get DJI's D-LogM to Rec.709 LUT from DJI) and DaVinci Resolve.
Save LUT writes to the LUT folder of the Mac App Store edition of Resolve; for another
edition, change `LUT_DIR` near the top of `app.py`.

Web links are fetched with yt-dlp. Use them as references to measure a look from; only put
footage on the timeline that you have the right to use.

This project is not affiliated with Blackmagic Design or DJI. Licence: [MIT](LICENSE).

## Why it works this way

The original goal was for an AI assistant to drive Resolve directly for colour grading. Two
facts ruled that out and shaped the design:

- **The free edition of Resolve blocks scripting from outside processes.** That needs
  Resolve Studio. (Checked on the Mac App Store edition, 21.1.)
- **Even in Studio, the scripting API cannot touch wheels, curves, qualifiers or windows.**
  For grading it can only apply LUTs and CDL values and copy grades.

So the colour work happens outside Resolve and is handed over as a LUT, which the free
edition loads like any other. Cutting and export were added later so that simple edits can
skip Resolve altogether.

## How to use it

1. **Your video**: paste a path or Browse. The preview plays it with the grade applied (Play,
   Replay, Space, arrow keys; drag on the scrub bar). The **Match frame** slider picks the frame
   the match is measured from; playing and scrubbing do not change it. For log footage, set an
   input LUT (e.g. the DJI D-LogM to Rec.709 LUT) so matching happens on converted frames.
2. **Reference**: paste a YouTube link or file path and pick the frame whose look you want.
3. **Match**: two strengths, one for brightness/contrast and one for colour.
4. **Look**: exposure, contrast, fade, saturation, temperature, tint, and shadow/highlight tints,
   applied after the match. **Texture** adds denoise and film grain to exported video; a saved
   LUT cannot carry them. Grain shows in the preview. To judge denoise, switch the preview to
   **Close-up**: it shows one spot of the current frame at full size, rendered by the exporter
   with and without denoise (about 1.6 s per update on the 4K test clip). Drag to look elsewhere.
5. **Cut and assemble** (optional): drag the In and Out markers on the scrub bar under the
   preview (or press I / O, use the Set buttons, or type a time such as `1:02.5`; Zoom + makes
   the bar cover less time for fine cuts), then click **Add clip**. Each clip keeps its
   own cut and grade. Click a clip to load it back and **Update selected**; ‹ › reorder; × removes.
6. **Export video**: renders the timeline to `~/Movies/Grade/<name>.mp4` (10-bit HEVC with
   audio). An existing file is never overwritten; a number is added to the name.
7. **Save LUT** (the Resolve route): writes `<name>.cube` into a `Grade` subfolder of Resolve's LUT folder. In
   Resolve, right-click the LUTs panel (Color page) and choose Refresh. If an input LUT was
   used, put the saved LUT on a node after it.

## Files

| File | What it is |
|---|---|
| `grade.py` | Colour maths and a command-line tool (`analyze`, `match`, `look`, `apply`). numpy + Pillow. |
| `app.py` | Web server, Python standard library only. Listens on `127.0.0.1:8791`. |
| `index.html` | The whole UI: one page, plain JavaScript, no build step. |
| `lutexport.swift` | GPU video exporter (Apple AVFoundation + Core Image). `app.py` compiles it to `lutexport` on first export. |
| `START_APP.md` | How to start and stop the app, and what to do when something fails. |
| `clips/` | Created on first use: one baked LUT and thumbnail per timeline clip. |
| `requirements.txt` | Python packages: numpy, Pillow, `yt-dlp[default]`. Installed into `.venv/`. |

External tools needed on the PATH: `ffmpeg`, `ffprobe`, and `node` (yt-dlp uses Node to solve
YouTube's challenges). `swiftc` (Xcode Command Line Tools) is optional: without it export
falls back to ffmpeg alone, about 3.5 times slower at 4K. There are no automated tests.

## How the colour maths works (`grade.py`)

All work is done in Oklab, on display-referred (Rec.709/sRGB) pixel values.

- **Statistics** (`stats`): lightness percentiles (0–100), mean chroma, and the mean colour
  cast in three smooth tonal bands (shadows, midtones, highlights).
- **Match** (`match_fn`):
  - Tone: a curve mapping source lightness percentiles onto the reference's. Its slope is
    capped by `MAX_SLOPE` (3×, and no flatter than 1/3) and re-anchored at the median.
  - Colour: removes the source's per-band cast, scales chroma by the ratio of mean chromas
    (clamped 0.5–2), and adds the reference's per-band cast.
  - `tone` and `colour` (0–1) blend each part independently.
- **Look** (`look_fn`): a fixed chain of exposure, contrast, fade, saturation,
  temperature/tint and shadow/highlight tints.
- **LUT**: the combined transform is sampled on a 33³ grid and written as `.cube`
  (red varies fastest). `apply_lut` does trilinear interpolation for previews.

## How the server works (`app.py`)

| Endpoint | Purpose |
|---|---|
| `GET /` | Serves `index.html` (read from disk on every request). |
| `POST /api/frame` | Grabs a frame (plus optional extra samples) for `source` or `reference`. |
| `GET /img/source`, `/img/reference` | The current frame as JPEG. |
| `POST /api/preview` | Builds the LUT from the slider values and returns the graded frame. |
| `POST /api/save` | Writes the LUT into Resolve's LUT folder. |
| `POST /api/pick` | Opens a native macOS file dialog via `osascript`. |
| `POST /api/clip`, `/api/clip/remove` | Bakes the current grade into `clips/<id>.cube` plus a thumbnail, or deletes them. |
| `POST /api/export`, `/api/export/cancel` | Starts or stops the one background export. |
| `GET /api/export` | Export state and progress; the page polls it every second. |
| `POST /api/closeup`, `GET /closeup/before.png`, `/closeup/after.png` | Renders three frames at the playhead with the GPU exporter, with and without denoise, and serves a full-size crop of each. |
| `GET /video/source` | The current local source file, with byte ranges, for the page's player. |
| `POST /api/lut`, `/api/cube` | The grade, or an input LUT file, as raw float32 rows for the player's shader. |

- **State is in memory**: the current frames, their statistics and resolved stream links.
  A restart clears it; the page restores the session from `localStorage` (key `grade`).
- **Frames**: `ffmpeg` extracts one frame, scaled to at most 960 px wide, using
  VideoToolbox hardware decoding (about 6× faster on the 4K HEVC test clip).
- **Sampling**: "Match from" can use the chosen frame only, frames within 5 s (default), or
  nine frames across the whole video. Extra frames feed the statistics only.
- **YouTube**: `yt-dlp` resolves a direct stream link and `ffmpeg` seeks into it, so only the
  needed frames are fetched. YouTube refuses a newly issued link for a few seconds, so the
  first grab retries the same link (up to 5 tries, 3 s apart).
- **Player**: the browser plays the source file in a hidden `<video>`; a WebGL2 shader draws
  each frame through the input LUT and the grade LUT (3D textures), so playback needs no
  server work. The stills from `/api/frame` remain for the histogram, and take over the
  viewer when the browser cannot play the file (web links, stills, unsupported codecs).
- **Denoise** (`lutexport.swift`): each pixel is averaged with the same pixel in the two frames
  before and after, weighted by how alike they are, so moving things are left alone. It runs
  before the LUT and costs no measurable render time. On the test clip's shaded sleeve it cut
  fine noise by about 13% at full strength. (Apple's own temporal noise filter reports itself
  unsupported on the M2 Max.)
- **Grain**: grey noise, new on every frame, added after the grade in proportion to l(1 − l), so
  mid-tones get the most and black and white none. Grain size is set for a 1080p picture and
  scales with the output. The same formula runs in the player's shader.
- **Clips**: the timeline lives in the page (`localStorage`); the server only holds each
  clip's baked LUT. With an input LUT, both are folded into one 64³ LUT (the GPU exporter's
  limit), because every LUT pass costs render time.
- **Export**: each clip's video is rendered by `lutexport` (decode, LUT and HEVC encode all on
  the GPU), then `ffmpeg` adds the audio; the parts are joined without re-encoding. `ffmpeg`
  alone (`lut3d` on the CPU) renders the whole timeline when `lutexport` cannot be built,
  cannot read one of the files, or any clip's frame rate differs from the first clip's. The
  engines are never mixed in one export: Apple's players cannot decode a file joined from
  parts made by both. Both paths were checked to
  cut on the same frames and give the same colours. For denoise and grain the ffmpeg path uses
  stand-ins (`hqdn3d`, and `noise` with a soft-light blend) tuned to a similar strength, not
  identical: its denoise also softens a little, and its grain is weaker in dark areas. The first clip sets the size and frame
  rate; other clips are scaled and padded to fit. Silent clips get a silent audio track.
- **Safety**: binds to localhost only and accepts POSTs only with a JSON content type, so
  other websites cannot drive it.

## Things that will surprise you

- **Save can hang on a macOS dialog.** Resolve's LUT folder is inside another app's sandbox
  container, so macOS asks to allow access to data from other apps and blocks the write until
  Allow is clicked. This can recur after each server restart.
- **Port 8791** is set by `PORT` near the top of `app.py`.
- **Still images cannot be seeked.** `ffprobe`'s format name is used to detect stills.
- **The grade is matched from the Match frame, not the playhead.** Use "Match from this
  frame" under the preview to move it. Only when the browser cannot play the file does
  scrubbing reload the still and so shift the match.
- **Export speed.** The GPU path rendered the full 24-minute 4K 60 fps test clip in 14
  minutes; the ffmpeg fallback takes about 2.2 s per second of video. A web-link clip has no
  sound (only the picture is fetched) and forces the fallback. Large exports need free disk space for the parts as well as the result.
- **The player's colours are close to, not identical to, the export.** The browser converts
  the video to RGB itself; measured channel averages were within 1.5 of 255 of the server's.
- **Editing `grade.py` or `app.py` needs a server restart**; editing `index.html` does not.

## Known limits

- **A LUT only remaps colours pixel by pixel.** Denoise and grain exist only in exported video;
  sharpening, windows and tracking stay manual in Resolve.
- **Grain makes files much larger** (it cannot be compressed away), and the encoder softens fine
  grain: below about 50% at size 1 little survives. Denoise can smooth faint real texture
  that barely moves.
- **Editing is cuts only.** No transitions, titles, audio levels or music, and no playback
  of the assembled timeline (only of the clip being cut). Cut points snap to 0.1 s.
- **Matching is statistical.** It works when the two shots have broadly similar content. With
  different content it over-corrects (in testing, a colour match turned red sleeves brownish);
  lower the Colour strength.
- **Rec.709 is assumed.** Log footage needs the input LUT. HDR (HLG/PQ) sources are not
  tone-mapped.
- **Statistics come from 8-bit, downscaled frames.** Measured on the test clip, this made no
  meaningful difference against a 16-bit path.
- **Noise is not caused by the LUT.** On the DJI D-LogM test clip, grain on shaded red sleeves
  was already in the camera file; the DJI conversion LUT raised it about 30% and the match
  added none.

## Verified by test

- **Mixed timelines**: clips of different sizes, frame rates (25, 30, 59.94), aspect ratios,
  with and without audio, stored turned 90°, 180° and 270°, a WebM clip and a YouTube link.
  Each clip's middle frame was compared with an independently built expected frame.
- **A full-length 4K export**: the 24-minute, 4K 59.94 fps DJI clip rendered on the GPU path in
  14 minutes, with all 86,619 frames and correct colours at six points along its length.
  The file was 17.8 GB, a little larger than the 16 GB original.
- **Apple playback**: every test export was decoded start to finish with AVFoundation, the
  engine QuickTime uses. QuickTime Player itself was not driven (it did not answer AppleScript).

- **Denoise and grain**: a three-clip 4K export (denoised, grainy, plain) on each engine. The
  plain clip matched the original's noise level, the denoised one was lower and the grainy one
  higher; grain left mean brightness unchanged and was colourless on a grey ramp.

- **Close-up**: rendered at two spots of the 4K test clip in the browser; the two sides
  differed with denoise on and the note said so with it off. It needs the GPU exporter and a
  local file (not a web link).

## Not yet verified

- The player in Safari and Chrome. It was tested in one Chromium-based browser; it needs
  WebGL2 and, for the DJI clips, HEVC playback, which both should have on a Mac.
- Loading a saved LUT inside Resolve and checking it matches the preview.
- The Browse buttons (they open a native dialog that the automated tests could not click).
- Real footage other than one DJI Osmo Action 6 D-LogM clip (the other test clips were
  generated test patterns), and any HDR footage.

## Ideas not built

- Play the assembled timeline in the page before exporting.
- Crossfades.

- Type a timestamp, or honour `?t=` in YouTube links, instead of dragging the slider.
- A shadow-desaturation slider to make shadow colour noise less visible.
- Limit how far the match moves already-saturated colours and skin tones.
- A bridge script run from inside Resolve (Workspace → Scripts) to grab stills and apply LUTs
  without manual steps. Whether the free edition allows this was never tested.

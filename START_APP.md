# Grade UI — how to start it

A local web app that colour-grades video, cuts and exports it, and can also save the grade
as a LUT for DaVinci Resolve. It runs as a plain
Python script on macOS. No Docker, no build step, no database.

## Facts

| | |
|---|---|
| Project folder | the folder containing this file |
| Start command | `.venv/bin/python app.py` (run from the project folder) |
| URL | http://127.0.0.1:8791 |
| Port | 8791 (set by `PORT` near the top of `app.py`) |
| Listens on | 127.0.0.1 only |

## Start

1. Check whether it is already running. If this prints `200`, skip to step 3.

   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8791/
   ```

2. From the project folder, start it in the background, logging to `grade-ui.log`.

   ```bash
   nohup .venv/bin/python app.py > grade-ui.log 2>&1 &
   ```

3. Open it.

   ```bash
   open http://127.0.0.1:8791
   ```

Success looks like: step 1's command prints `200` and the page titled "Grade" loads.

## Stop

```bash
lsof -ti tcp:8791 | xargs kill
```

## If something fails

- **"Address already in use"**: another copy is already running (use it, or stop it with the
  command above). If a different program holds port 8791, change `PORT` in `app.py` to a free
  port and use that port in the URL.
- **`.venv` is missing or broken**: recreate it from the project folder.

  ```bash
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
  ```

- **Frames fail to load**: the app needs `ffmpeg` and `ffprobe` on the PATH (installed with
  Homebrew at `/opt/homebrew/bin`). YouTube references also need `node` on the PATH.
- **A YouTube reference is slow**: normal. The first frame from a new link takes about
  7 seconds because YouTube briefly refuses new stream links and the app retries.
- **Save LUT hangs**: Resolve's LUT folder belongs to another app, so macOS shows a dialog
  asking to allow access to data from other apps. The save waits until the user clicks Allow.
  The dialog can reappear each time the server is restarted.
- **Browse buttons do nothing**: they open a native macOS file dialog from the server
  process, so the app must be started in the logged-in user's desktop session, not over SSH.

## What the files are

- `app.py`: the web server (Python standard library only).
- `index.html`: the single-page UI it serves.
- `grade.py`: the colour maths; also usable as a command-line tool (`.venv/bin/python grade.py -h`).
- `lutexport.swift`: GPU video exporter; `app.py` compiles it to `lutexport` on the first
  export if `swiftc` is installed, and uses ffmpeg alone otherwise.
- `.venv/`: Python environment with numpy, Pillow and yt-dlp.

## What the app does once it is open

1. **Your video**: paste a file path or click Browse, then pick a frame with the slider.
   Optionally set an input LUT (for log footage such as DJI D-LogM) in the field below it.
2. **Reference**: paste a YouTube link or a file path and pick the frame whose look you want.
3. **Match / Look**: adjust the strength and look sliders; the preview updates live.
4. **Timeline / Export video** (optional): play the video in the preview, drag the In and Out
   markers on the scrub bar, click Add clip for each shot, then
   Export video. The file is written to `~/Movies/Grade/`.
5. **Save LUT**: writes a `.cube` file to Resolve's LUT folder, in a `Grade` subfolder:
   `~/Library/Containers/com.blackmagic-design.DaVinciResolveLite/Data/Library/Application Support/LUT/Grade/`
   In Resolve, right-click the LUTs panel on the Color page and choose Refresh to see it.

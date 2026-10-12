# Media

Screen recordings and screenshots of the Stacks Console dashboard for the README. Everything here
was captured from a real, freshly started installation. No frame was edited. The only changes were
cutting the recording and speeding parts of it up.

| File | Size | What |
|---|---|---|
| `hero.gif` | 1000×625, 12 fps, 19.9 s | First sign-in → choose games → add a Minecraft server → create the container → enable and start → boot log → console `list` → backup → fleet with two running servers |
| `hero.mp4` | 1280×800, 25 fps, 19.9 s | The same cut as H.264, for places that play video |
| `fleet.png` | 1280×800 | Fleet view: a Minecraft and a Factorio server running |
| `add-server.png` | 1280×1000 | Add server form with the Minecraft preset |
| `server-home.png` | 1280×1500 | Server page: connect address and the open Reachability panel after "Check from inside" |

## How they were made

- **Installation:**
  - `dashboard/` from this repository was started with `docker compose up -d --build` under its
    own compose project.
  - The `.env` had generated secrets and `PUBLIC_HOST=play.example.com`, with
    `DATA_ROOT=/tmp/gss-demo`.
  - Everything else was left at the defaults from `.env.example`, in the light theme and in
    English.
- **Servers:**
  - Two servers, created through the UI exactly as shown.
  - "Friends SMP": Minecraft (Paper 26.2) on port 25011/tcp, 4096 MB memory limit and a 3072 MB
    heap.
  - "Factory Night": Factorio 2.0.77 on port 25012/udp.
  - The names are made up. No player ever joined, so no player names appear.
- **Recording:**
  - Playwright 1.63 (Chromium) at a 1280×800 viewport, using its built-in video recording.
  - A small overlay script draws a pointer circle where the mouse is, so clicks can be followed.
    It only runs in the recording browser and is not part of the dashboard.
- **Cut:**
  - ffmpeg 6.1 trimmed the recordings and sped up waits and typing by 1.25× to 6.5×.
  - The almost four-minute Minecraft first boot was reduced to its last two seconds.
  - The GIF was then encoded at 12 fps with a 128-colour palette (`palettegen`/`paletteuse`,
    no dithering) and compressed with `gifsicle -O3 --lossy=25`.
- **Stills:** Playwright screenshots of the viewport, taken after the page had finished loading.

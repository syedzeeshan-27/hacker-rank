"""Record a short walkthrough of the results viewer and encode it with ffmpeg.

    python code/tools/record_demo.py                      # viewer/index.html -> demo/demo.mp4 + demo/demo.gif
    python code/tools/record_demo.py --no-gif

How: Playwright drives the page and takes a screenshot after every action (a storyboard with hold
times); the system ffmpeg stitches the frames into an mp4 and a gif. No Playwright ffmpeg build needed.
Needs: playwright (pip) with a Chromium build, ffmpeg on PATH. If Playwright's browsers live in a
non-default folder, set PLAYWRIGHT_BROWSERS_PATH first. Demo tooling only; not part of the audit.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VIEWER = ROOT / "viewer" / "index.html"
DEMO_DIR = ROOT / "demo"
RAW_DIR = DEMO_DIR / "raw"
W, H = 1440, 900


def storyboard(viewer: Path) -> list[tuple[Path, float]]:
    """Drive the viewer; return [(frame_path, hold_seconds), ...]."""
    from playwright.sync_api import sync_playwright

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for old in RAW_DIR.glob("frame_*.png"):
        old.unlink()
    frames: list[tuple[Path, float]] = []

    def shot(hold: float) -> None:
        path = RAW_DIR / f"frame_{len(frames):03d}.png"
        page.screenshot(path=str(path))
        frames.append((path, hold))

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H}, color_scheme="light")
        page.goto(viewer.resolve().as_uri())
        page.wait_for_selector(".row")
        time.sleep(0.3)
        shot(2.5)                                                  # overview
        page.click('.chip[data-f="INCOMPLETE"]'); time.sleep(0.2); shot(1.5)
        for i in (0, 2, 4):                                        # a few incomplete cases
            if i < page.locator(".row").count():
                page.locator(".row").nth(i).click(); time.sleep(0.2); shot(2.2)
        page.click('.chip[data-f="COMPLETE"]'); time.sleep(0.2); shot(1.2)
        for i in (0, 1):
            if i < page.locator(".row").count():
                page.locator(".row").nth(i).click(); time.sleep(0.2); shot(2.0)
        page.click('.chip[data-f="NOT_APPLICABLE"]'); time.sleep(0.2); shot(1.2)
        if page.locator(".row").count():
            page.locator(".row").nth(0).click(); time.sleep(0.2); shot(1.8)
        page.click('.chip[data-f="ALL"]')
        page.fill("#q", "backup_service"); time.sleep(0.3); shot(1.5)
        if page.locator(".row").count():
            page.locator(".row").nth(0).click(); time.sleep(0.2); shot(2.2)
        page.fill("#q", ""); time.sleep(0.2)
        page.mouse.wheel(0, 900); time.sleep(0.4); shot(2.5)         # findings by system
        browser.close()
    return frames


def encode(frames: list[tuple[Path, float]], gif: bool) -> list[Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg not found on PATH")
    concat = RAW_DIR / "frames.txt"
    lines = []
    for path, hold in frames:
        lines.append(f"file '{path.name}'")
        lines.append(f"duration {hold:.2f}")
    lines.append(f"file '{frames[-1][0].name}'")                    # concat demuxer needs the last file twice
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = []
    mp4 = DEMO_DIR / "demo.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                    "-vf", "fps=30,scale=1280:-2:flags=lanczos,format=yuv420p", "-c:v", "libx264", "-crf", "22",
                    "-movflags", "+faststart", str(mp4)], check=True, cwd=str(RAW_DIR))
    out.append(mp4)
    if gif:
        palette = RAW_DIR / "palette.png"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-vf", "fps=8,scale=960:-1:flags=lanczos,palettegen=max_colors=128", str(palette)],
                       check=True, cwd=str(RAW_DIR))
        g = DEMO_DIR / "demo.gif"
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-i", str(palette), "-lavfi",
                        "fps=8,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4", str(g)],
                       check=True, cwd=str(RAW_DIR))
        out.append(g)
    return out


def probe(path: Path) -> str:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return ""
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                        str(path)], capture_output=True, text=True)
    return r.stdout.strip()


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--viewer", default=str(VIEWER))
    p.add_argument("--no-gif", action="store_true")
    a = p.parse_args(argv)
    viewer = Path(a.viewer)
    if not viewer.exists():
        raise SystemExit(f"viewer not found: {viewer} (run code/tools/build_viewer.py first)")
    frames = storyboard(viewer)
    files = encode(frames, gif=not a.no_gif)
    print(f"{len(frames)} frames, {sum(h for _, h in frames):.1f} s storyboard")
    for f in files:
        print(f"{f.relative_to(ROOT)}  {f.stat().st_size // 1024} KB  duration={probe(f) or '?'}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""final.mp4 に BGM レイヤが実在するかを検証(QA5: 5QA ゲートの末端)。

二段ガード:
  G-Compose: compose.py の ffprobe sanity (audio channels>=2)
  G-QA(本script): final.mp4 を独立に再検査(channels>=2 + 簡易 loudness 測定)

判定:
  PASS: audio channels>=2 かつ overall loudness が概ね speech-only 想定下限を上回る
  FAIL: channels==1 (BGM mix漏れ確定) もしくは loudness が極端に低い
        (音声単体の場合と比較して BGM レイヤが乗っていない疑い)

依存: ffprobe(必須), ffmpeg(loudness測定; 任意)。
log.yaml.bgm.choice/file は参考表示のみ。

CLI:
  .venv/bin/python src/qa_bgm_presence.py --topic <slug> [--channel <name>] [--video <path>]
exit: 0=PASS, 1=FAIL, 2=video not found, 3=stale (mtime < script.json)
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import videos_root, add_channel_arg  # noqa: E402

# loudness が -50 LUFS より静かなら mix 漏れ濃厚 (speech のみでも -25〜-20 LUFS は出る)
LOUDNESS_FLOOR_LUFS = -45.0


def probe_audio(video: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels,channel_layout,sample_rate,codec_name",
         "-of", "default=nw=1", str(video)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"error": r.stderr[-500:]}
    info = {}
    for line in r.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k] = v
    return info


def measure_loudness(video: Path) -> float | None:
    """ebur128 で integrated loudness (LUFS) を測定。失敗時 None。"""
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video),
         "-map", "0:a:0", "-af", "ebur128=peak=true",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = r.stderr
    # "I:         -23.4 LUFS" 末尾の Summary を拾う
    matches = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", log)
    if not matches:
        return None
    try:
        return float(matches[-1])
    except ValueError:
        return None


def read_log_yaml(topic_dir: Path) -> dict:
    p = topic_dir / "log.yaml"
    if not p.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(p.read_text()) or {}
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--video", default=None)
    ap.add_argument("--loudness-floor", type=float, default=LOUDNESS_FLOOR_LUFS,
                    help=f"これより静かなら BGM 抜け疑い (default={LOUDNESS_FLOOR_LUFS} LUFS)")
    add_channel_arg(ap)
    args = ap.parse_args()

    topic_dir = videos_root(args.channel) / args.topic
    video = Path(args.video) if args.video else (topic_dir / "final.mp4")
    if not video.exists():
        print(f"video not found: {video}", file=sys.stderr)
        sys.exit(2)
    script_path = topic_dir / "script.json"
    if script_path.exists() and video.stat().st_mtime < script_path.stat().st_mtime:
        print(f"[STALE] {video.name} mtime < script.json mtime → 再composeが必要", file=sys.stderr)
        sys.exit(3)
    print(f"[QA target] {video} (size={video.stat().st_size:,}B mtime={int(video.stat().st_mtime)})")

    log = read_log_yaml(topic_dir)
    bgm_choice = (log.get("bgm") or {}).get("file") or (log.get("bgm") or {}).get("choice")
    if bgm_choice:
        print(f"  log.yaml bgm.choice: {bgm_choice}")

    info = probe_audio(video)
    if "error" in info:
        print(f"FAIL ffprobe: {info['error']}", file=sys.stderr)
        out_write(topic_dir, {"verdict": "FAIL", "reason": "ffprobe failed", "ffprobe": info})
        sys.exit(1)
    channels = int(info.get("channels", "0") or 0)
    print(f"  audio: codec={info.get('codec_name')} channels={channels} layout={info.get('channel_layout','?')} sr={info.get('sample_rate','?')}")

    loud = measure_loudness(video)
    if loud is not None:
        print(f"  integrated loudness: {loud:.1f} LUFS (floor={args.loudness_floor})")
    else:
        print("  loudness: measurement skipped (ebur128 unavailable)")

    reasons = []
    if channels < 2:
        reasons.append(f"audio channels={channels} (<2; BGM mix not stereo)")
    if loud is not None and loud < args.loudness_floor:
        reasons.append(f"loudness {loud:.1f} LUFS < floor {args.loudness_floor}")

    verdict = "FAIL" if reasons else "PASS"
    out = {
        "topic": args.topic,
        "video": str(video),
        "channels": channels,
        "channel_layout": info.get("channel_layout"),
        "loudness_lufs": loud,
        "loudness_floor": args.loudness_floor,
        "bgm_choice": bgm_choice,
        "verdict": verdict,
        "reasons": reasons,
    }
    out_write(topic_dir, out)
    print(f"=== bgm presence ===")
    print(f"verdict: {verdict}")
    for r in reasons:
        print(f"  - {r}")
    sys.exit(0 if verdict == "PASS" else 1)


def out_write(topic_dir: Path, data: dict) -> None:
    p = topic_dir / "output" / "bgm_presence.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""final.mp4(or av_only.mp4)に「映像が止まる」フリーズ区間が無いか検証(G3)。
ffmpeg freezedetect フィルタで動かないフレームを検出。
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


def detect_freezes(video: Path, noise: float = 0.001, min_dur: float = 0.5) -> list[dict]:
    """freezedetect で固定フレーム区間を検出。
    noise: 微小変動の閾値(0.001=ほぼ静止のみ)
    min_dur: 報告対象の最短継続秒
    """
    r = subprocess.run(
        [
            "ffmpeg", "-i", str(video),
            "-vf", f"freezedetect=n={noise}:d={min_dur}",
            "-map", "0:v:0", "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    log = r.stderr
    starts = re.findall(r"freeze_start: ([\d.]+)", log)
    durations = re.findall(r"freeze_duration: ([\d.]+)", log)
    ends = re.findall(r"freeze_end: ([\d.]+)", log)
    freezes = []
    for i, s in enumerate(starts):
        freezes.append({
            "start": float(s),
            "end": float(ends[i]) if i < len(ends) else None,
            "duration": float(durations[i]) if i < len(durations) else None,
        })
    return freezes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--video", default=None)
    ap.add_argument("--noise", type=float, default=0.001)
    ap.add_argument("--min-dur", type=float, default=0.5)
    ap.add_argument("--max-allowed", type=float, default=1.0,
                    help="許容最大フリーズ秒(これ超で FAIL)")
    add_channel_arg(ap)
    args = ap.parse_args()

    topic_dir = videos_root(args.channel) / args.topic
    # 既定の正本はトピック直下 final.mp4 (compose.py の書込先と一致)。
    # 旧 output/final.mp4 はキャッシュ不整合の元
    video = Path(args.video) if args.video else (topic_dir / "final.mp4")
    if not video.exists():
        print(f"video not found: {video}", file=sys.stderr)
        sys.exit(2)
    # 鮮度ゲート: script.json より古い動画に対する QA は無意味なので即停止
    script_path = topic_dir / "script.json"
    if script_path.exists() and video.stat().st_mtime < script_path.stat().st_mtime:
        print(f"[STALE] {video.name} mtime < script.json mtime → 再composeが必要", file=sys.stderr)
        sys.exit(3)
    print(f"[QA target] {video} (size={video.stat().st_size:,}B mtime={int(video.stat().st_mtime)})")

    freezes = detect_freezes(video, args.noise, args.min_dur)

    bad = [f for f in freezes if (f["duration"] or 0) >= args.max_allowed]
    out = {
        "topic": args.topic,
        "video": str(video),
        "noise": args.noise,
        "min_dur": args.min_dur,
        "max_allowed": args.max_allowed,
        "freezes_total": len(freezes),
        "freezes_over_threshold": len(bad),
        "freezes": freezes,
        "verdict": "PASS" if not bad else "FAIL",
    }
    out_path = topic_dir / "output" / "freeze_check.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print(f"=== freeze check ===")
    print(f"verdict: {out['verdict']}")
    print(f"freezes total: {len(freezes)}, over {args.max_allowed}s: {len(bad)}")
    for f in bad[:10]:
        print(f"  freeze {f['start']:.2f}-{f['end']:.2f}s ({f['duration']:.2f}s)")

    if bad:
        sys.exit(1)


if __name__ == "__main__":
    main()

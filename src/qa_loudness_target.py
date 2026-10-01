"""qa_loudness_target.py — 最終 mp4 の loudness 規格適合チェック。

audio_master.py の規格(全ch一律):
  - integrated loudness: -16 LUFS ± 2.5 dB(amix BGM 込みでもこの範囲内に収まる想定)
  - true peak:           <= -0.3 dBFS(alimiter limit=0.95 → 約 -0.45 dBFS)

旧 qa_bgm_presence.py は BGM 抜け検出(-45 LUFS floor)に特化しているため
こちらは「規格通りに -16 LUFS で出てきているか」を別観点で監査する。

target / tolerance を超えた場合は FAIL を返す(再 compose 推奨)。
出力: <topic_dir>/output/loudness_target.json
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

TARGET_LUFS = -16.0
TOLERANCE_DB = 2.5          # 許容 ±2.5 dB(BGM 込みなので少し広め)
TRUE_PEAK_CEILING_DBFS = -0.3   # alimiter limit=0.95 ≒ -0.45 dBFS、計測誤差を見込んで -0.3


def measure_ebur128(video: Path) -> dict:
    """ffmpeg ebur128=peak=true で integrated loudness と true peak を取得する。"""
    r = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-i", str(video),
         "-map", "0:a:0", "-af", "ebur128=peak=true",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = r.stderr or ""
    out: dict = {}
    m_i = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", log)
    if m_i:
        try:
            out["integrated_lufs"] = float(m_i[-1])
        except ValueError:
            pass
    # ebur128 の summary に "Peak: -0.5 dBFS" or "True peak: -0.3 dBFS" が含まれる
    m_p = re.findall(r"(?:True peak|Peak):\s*(-?\d+(?:\.\d+)?)\s*dBFS", log)
    if m_p:
        try:
            out["true_peak_dbfs"] = float(m_p[-1])
        except ValueError:
            pass
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="audio_master 規格(-16 LUFS / TP -0.3)適合を監査")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--video", default=None,
                    help="検査対象 mp4(default: <topic>/output/final.mp4 → 無ければ short_A.mp4)")
    ap.add_argument("--target-lufs", type=float, default=TARGET_LUFS)
    ap.add_argument("--tolerance-db", type=float, default=TOLERANCE_DB)
    ap.add_argument("--peak-ceiling-dbfs", type=float, default=TRUE_PEAK_CEILING_DBFS)
    add_channel_arg(ap)
    args = ap.parse_args()

    topic_dir = videos_root(args.channel) / args.topic
    if args.video:
        video = Path(args.video)
    else:
        cand = topic_dir / "output" / "final.mp4"
        if not cand.exists():
            cand = topic_dir / "output" / "short_A.mp4"
        video = cand
    if not video.exists():
        print(f"[ERROR] video not found: {video}", file=sys.stderr)
        sys.exit(2)

    print(f"[qa_loudness_target] target={args.target_lufs}±{args.tolerance_db}dB, "
          f"TP<={args.peak_ceiling_dbfs}dBFS, file={video}", file=sys.stderr)

    meas = measure_ebur128(video)
    li = meas.get("integrated_lufs")
    tp = meas.get("true_peak_dbfs")

    reasons = []
    if li is None:
        reasons.append("integrated loudness の計測に失敗(ebur128 出力 parse 不能)")
    else:
        diff = li - args.target_lufs
        if abs(diff) > args.tolerance_db:
            reasons.append(
                f"integrated {li:.1f} LUFS は target {args.target_lufs} ±{args.tolerance_db} dB "
                f"の範囲外(差分 {diff:+.1f} dB)"
            )

    if tp is not None and tp > args.peak_ceiling_dbfs:
        reasons.append(
            f"true peak {tp:.2f} dBFS > ceiling {args.peak_ceiling_dbfs} dBFS "
            f"(alimiter が効いていない疑い)"
        )

    verdict = "FAIL" if reasons else "PASS"
    out = {
        "topic": args.topic,
        "video": str(video),
        "integrated_lufs": li,
        "true_peak_dbfs": tp,
        "target_lufs": args.target_lufs,
        "tolerance_db": args.tolerance_db,
        "peak_ceiling_dbfs": args.peak_ceiling_dbfs,
        "verdict": verdict,
        "reasons": reasons,
    }
    out_dir = topic_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "loudness_target.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print(f"=== qa_loudness_target ===")
    print(f"  integrated: {li if li is not None else '?'} LUFS (target {args.target_lufs}±{args.tolerance_db})")
    print(f"  true peak : {tp if tp is not None else '?'} dBFS (ceiling {args.peak_ceiling_dbfs})")
    print(f"verdict: {verdict}")
    for r in reasons:
        print(f"  - {r}")
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()

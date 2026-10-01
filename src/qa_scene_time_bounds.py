"""scene_time + line_duration が source 尺を超えていないか検証(G1)。
超えていると ffmpeg trim が末尾フレームをloopさせて「映像が止まる」事故が起きる。
av_only ビルド前に必ず実行。
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import videos_root, add_channel_arg  # noqa: E402


def get_source_duration(path: Path) -> float:
    """ffprobeで動画の尺を秒で返す。画像は無限扱い(0なら未知)。"""
    if not path.exists():
        return 0.0
    if path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
        return 1e9  # 画像はKenBurnsでlineに合わせて生成するので実質無限
    import subprocess
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--margin", type=float, default=0.5,
                    help="source 末尾からのマージン秒")
    add_channel_arg(ap)
    args = ap.parse_args()

    topic_dir = videos_root(args.channel) / args.topic
    scenes = json.loads((topic_dir / "scenes.json").read_text())
    timings_path = topic_dir / "work" / "timings.json"

    if timings_path.exists():
        timings = json.loads(timings_path.read_text())
    else:
        # timings.json がまだなら script.json を読む
        # line_duration の仮定値: channel.yaml content.script_duration_sec / script_lines から算出(fallback=4.5s)
        script = json.loads((topic_dir / "script.json").read_text())
        try:
            from channel_resolver import channel_config, current_channel  # noqa: E402
            ch_cfg = channel_config(current_channel(args.channel))
            content = ch_cfg.get("content") or {}
            dur_rng = content.get("script_duration_sec") or {}
            lines_rng = content.get("script_lines") or {}
            if dur_rng.get("min") and lines_rng.get("max"):
                default_line_dur = round(dur_rng["min"] / lines_rng["max"], 2)
            else:
                default_line_dur = 4.5
        except Exception:
            default_line_dur = 4.5
        timings = [{**l, "duration": default_line_dur} for l in script["lines"]]

    _raw_sources = scenes["sources"]
    if isinstance(_raw_sources, list):
        sources = {item["id"]: {k: v for k, v in item.items() if k != "id"} for item in _raw_sources}
    else:
        sources = _raw_sources
    src_durations: dict = {}
    for sid, meta in sources.items():
        kind = meta.get("kind", "video")
        path_str = meta.get("path", "")
        if not path_str:
            src_durations[sid] = 1e9
            continue
        path = ROOT / path_str
        if "duration" in meta and kind == "video":
            src_durations[sid] = float(meta["duration"])
        else:
            src_durations[sid] = get_source_duration(path)

    # scene_continue チェーンでは実際の src_start は前ラインの src_end
    cum_src: dict = {}
    failures = []
    for i, line in enumerate(timings):
        sid = line.get("scene_source")
        if not sid or sid not in src_durations:
            continue
        dur = float(line["duration"])
        src_dur = src_durations[sid]
        if src_dur > 1e8:  # 画像
            continue
        # scene_continue チェーンの実 src_start を計算
        if line.get("scene_continue") and sid in cum_src:
            st = cum_src[sid]
        else:
            st = float(line.get("scene_time") or 0)
            if st < 0:
                # -1 等は「ソース先頭から」の意図 → bounds チェック対象外
                cum_src[sid] = dur
                continue
        cum_src[sid] = st + dur
        end = st + dur
        if end > src_dur - args.margin:
            failures.append({
                "line_index": i + 1,
                "scene_source": sid,
                "scene_time": st,
                "duration": dur,
                "end": round(end, 2),
                "source_duration": round(src_dur, 2),
                "overflow": round(end - src_dur, 2),
                "fix_hint": f"scene_time を {round(src_dur - dur - args.margin, 1)} 以下に",
            })

    out = {
        "topic": args.topic,
        "margin": args.margin,
        "source_durations": src_durations,
        "failures": failures,
        "verdict": "PASS" if not failures else "FAIL",
    }
    out_path = topic_dir / "output" / "scene_time_bounds.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print(f"=== scene_time bounds check ===")
    print(f"verdict: {out['verdict']}")
    print(f"failures: {len(failures)}")
    for f in failures[:10]:
        print(f"  L{f['line_index']:02d} {f['scene_source']:25s} "
              f"st={f['scene_time']} dur={f['duration']:.1f} "
              f"→ end={f['end']} > src={f['source_duration']} (+{f['overflow']})")
        print(f"    {f['fix_hint']}")

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()

"""qa_freezedetect: ffmpeg freezedetect で final.mp4 のフリーズ箇所(同一フレーム連続)を機械検出。

stage3_full 承認前の必須ゲート(qa.md 1ab)。
0.5秒以上のフリーズが見つかれば FAIL_REWORK_PRODUCTION、0.1-0.5sはWARN。

--profile av_only|main|shorts を指定すると config/qa_profile.yaml から severe_threshold を
自動取得する。--severe-threshold で明示 override も可能。
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import videos_root, add_channel_arg  # noqa: E402
FFMPEG = os.environ.get("FFMPEG_BIN") or "ffmpeg"

# profile → severe_threshold のデフォルトマッピング(yaml が読めない場合のフォールバック)
_PROFILE_DEFAULTS: dict[str, float] = {
    "av_only": 5.0,
    "main": 1.0,
    "shorts": 2.0,
}


def _load_severe_threshold(profile: str) -> float:
    """config/qa_profile.yaml から severe_threshold を取得する。失敗時はデフォルト値を返す。"""
    # profile ごとに対応する yaml キー(main は final_threshold_sec を severe 閾値として使う)
    _PROFILE_YAML_KEY: dict[str, str] = {
        "av_only": "av_only_threshold_sec",
        "main": "final_threshold_sec",
        "shorts": "shorts_threshold_sec",
    }
    try:
        import yaml  # type: ignore
        cfg_path = ROOT / "config" / "qa_profile.yaml"
        profiles = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["profiles"]
        pdata = profiles[profile]
        freeze = pdata.get("freeze", {})
        key = _PROFILE_YAML_KEY.get(profile)
        if key and key in freeze:
            return float(freeze[key])
    except Exception as e:
        print(f"[qa_freezedetect] WARN: qa_profile.yaml 読み込み失敗({e}), デフォルト値を使用", file=sys.stderr)
    return _PROFILE_DEFAULTS.get(profile, 1.0)


def detect_freezes(mp4: Path, threshold_n: float = 0.001, min_dur: float = 0.1) -> list[dict]:
    """freezedetect 実行 → 検出した freeze の (start, end, dur) 一覧を返す"""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "info",
        "-i", str(mp4),
        "-vf", f"freezedetect=n={threshold_n}:d={min_dur}",
        "-map", "0:v:0", "-f", "null", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    starts = re.findall(r"freeze_start: ([\d.]+)", out)
    ends = re.findall(r"freeze_end: ([\d.]+)", out)
    durs = re.findall(r"freeze_duration: ([\d.]+)", out)
    freezes = []
    for s, e, d in zip(starts, ends, durs):
        freezes.append({"start": float(s), "end": float(e), "duration": float(d)})
    return freezes


def verdict(freezes: list[dict], severe_threshold: float = 0.5) -> tuple[str, list[str]]:
    """severe_threshold: この秒数以上をFAIL対象とする(av_only bubble静止=5.0s等)"""
    severe = [f for f in freezes if f["duration"] >= severe_threshold]
    mild = [f for f in freezes if 0.1 <= f["duration"] < severe_threshold]
    msgs = []
    if severe:
        msgs.append(f"{len(severe)}件の長期フリーズ(≥{severe_threshold}s)")
        for f in severe:
            msgs.append(f"  - t={f['start']:.2f}-{f['end']:.2f}s ({f['duration']:.2f}s)")
    if mild:
        msgs.append(f"{len(mild)}件の短フリーズ(0.1-{severe_threshold}s未満)")
    if severe:
        return "FAIL_REWORK_PRODUCTION", msgs
    if mild:
        return "WARN", msgs
    return "PASS", ["フリーズなし"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--target", default="final.mp4", help="検査対象 (final.mp4 / output/av_only.mp4)")
    ap.add_argument("--n", type=float, default=0.001, help="ピクセル差閾値")
    ap.add_argument("--d", type=float, default=0.1, help="ffmpeg freezedetect filter の最短報告秒(min_dur)。severe判定とは独立。")
    ap.add_argument(
        "--profile",
        choices=list(_PROFILE_DEFAULTS.keys()),
        default=None,
        help="QAプロファイル(av_only/main/shorts)。指定するとqa_profile.yamlからsevere_thresholdを取得。",
    )
    ap.add_argument(
        "--severe-threshold",
        type=float,
        default=None,
        dest="severe_threshold",
        help="severe判定の閾値秒(明示override)。--profile より優先。",
    )
    add_channel_arg(ap)
    args = ap.parse_args()

    # severe_threshold の決定: 明示 > profile > デフォルト0.5s
    if args.severe_threshold is not None:
        severe_threshold = args.severe_threshold
    elif args.profile is not None:
        severe_threshold = _load_severe_threshold(args.profile)
        print(f"[qa_freezedetect] profile={args.profile} → severe_threshold={severe_threshold}s", file=sys.stderr)
    else:
        severe_threshold = 0.5
        print(
            "[qa_freezedetect] WARN: --profile 未指定。severe_threshold=0.5s を使用。"
            " run_qa_gates.py 経由で --profile を指定することを推奨。",
            file=sys.stderr,
        )

    base = videos_root(args.channel) / args.topic
    if not base.exists():
        sys.exit(f"topic not found: {base}")

    target = base / args.target if not args.target.startswith("/") else Path(args.target)
    if not target.exists():
        sys.exit(f"target not found: {target}")

    print(f"[qa_freezedetect] {target.name} を検査中... (min_dur={args.d}s, severe_threshold={severe_threshold}s)")
    freezes = detect_freezes(target, args.n, args.d)
    v, msgs = verdict(freezes, severe_threshold=severe_threshold)

    out = base / "output" / "freezedetect_qa.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "verdict": v,
        "freezes": freezes,
        "params": {
            "n": args.n,
            "d": args.d,
            "severe_threshold": severe_threshold,
            "profile": args.profile,
            "target": str(target),
        },
    }, ensure_ascii=False, indent=2))

    print(f"[qa_freezedetect] verdict={v}")
    for m in msgs:
        print(f"  {m}")
    print(f"[qa_freezedetect] report: {out}")
    if v.startswith("FAIL"):
        sys.exit(2)


if __name__ == "__main__":
    main()

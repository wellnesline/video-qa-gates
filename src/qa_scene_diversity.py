"""シーン多様性 QA(ポストビルド)。

prebuild_layers.check_scene_diversity と同じルールを最終 timings.json
(=実際にcomposeに使われた値) に対して再検証する。ビルド前ゲートをすり抜けた
場合や、手で work/timings.json を書き換えた場合の安全網。

検査対象:
  - 画像 source 出現回数 ≤ 1(使い切り、再使用禁止)
  - 同 source 隣接連続 ≤ 3
  - 動画 source 内の時間レンジ重複(scene_continue 隣接以外NG)
  - 動画クリップ最低尺 ≥ 1.0s(scene_continue 以外)

出力: videos/<topic>/output/scene_diversity.json
exit: 違反があれば 1
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "src"))
from prebuild_layers import check_scene_diversity  # type: ignore  # noqa: E402
from channel_resolver import videos_root, add_channel_arg  # type: ignore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    add_channel_arg(ap)
    args = ap.parse_args()

    topic_dir = videos_root(args.channel) / args.topic
    scenes = json.loads((topic_dir / "scenes.json").read_text())
    _raw_sources = scenes["sources"]
    if isinstance(_raw_sources, list):
        sources = {item["id"]: {k: v for k, v in item.items() if k != "id"} for item in _raw_sources}
    else:
        sources = _raw_sources

    timings_path = topic_dir / "work" / "timings.json"
    if timings_path.exists():
        timings = json.loads(timings_path.read_text())
        source_label = "work/timings.json"
    else:
        script = json.loads((topic_dir / "script.json").read_text())
        timings = [{**l, "duration": float(l.get("duration", 4.5))} for l in script["lines"]]
        source_label = "script.json (timings未生成・durationフォールバック)"

    violations = check_scene_diversity(sources, timings)

    out = {
        "topic": args.topic,
        "source": source_label,
        "violations": violations,
        "verdict": "PASS" if not violations else "FAIL",
    }
    out_path = topic_dir / "output" / "scene_diversity.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print("=== scene diversity QA ===")
    print(f"source : {source_label}")
    print(f"verdict: {out['verdict']}")
    print(f"violations: {len(violations)}")
    for v in violations[:20]:
        print(f"  - {v}")

    if violations:
        sys.exit(1)


if __name__ == "__main__":
    main()

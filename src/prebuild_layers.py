"""synthetic stub: simplified scene-diversity check (the real prebuild_layers is not included).

Implements only two of the rules described in qa_scene_diversity.py, on a tolerant
data shape, so that the post-build QA script can run end to end:
  - an image source may appear at most once
  - the same source may not appear more than 3 times in a row
timings items are expected to carry the source id under "source" / "source_id" / "broll_source".
"""
from __future__ import annotations

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _is_image(meta: dict) -> bool:
    path = str(meta.get("path") or meta.get("file") or meta.get("url") or "")
    return str(meta.get("type", "")).lower() == "image" or path.lower().endswith(IMAGE_EXTS)


def _sid(item: dict):
    return item.get("source") or item.get("source_id") or item.get("broll_source")


def check_scene_diversity(sources: dict, timings: list[dict]) -> list[str]:
    violations: list[str] = []
    counts: dict[str, int] = {}
    run_src, run_len = None, 0
    for item in timings:
        sid = _sid(item)
        if not sid:
            run_src, run_len = None, 0
            continue
        counts[sid] = counts.get(sid, 0) + 1
        if sid == run_src:
            run_len += 1
        else:
            run_src, run_len = sid, 1
        if run_len > 3:
            violations.append(f"source {sid}: 隣接連続 {run_len} > 3")
    for sid, n in counts.items():
        if n > 1 and _is_image(sources.get(sid, {})):
            violations.append(f"image source {sid}: 出現 {n} 回 > 1")
    return violations
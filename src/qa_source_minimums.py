"""qa_source_minimums.py — scenes.json の sources 構成ハードガード。

チェック項目:
  BLOCK: A(kind=video) ≥ 1 必須
  BLOCK: AI画像(ai_* prefix or ai_generated=true) < 100% of total
  BLOCK: AI画像枚数 ≤ channel.yaml の content.ai_image.max_per_video
  WARN:  AI画像比率 > 60% → 警告(ブロックではない)

環境変数 ALLOW_AI_HEAVY=1 でBLOCKをWARNに降格(緊急バイパス)。

注意: 本ゲートは「動画素材(kind=video)を最低1本使う企画」を前提にした例。
動画素材は、ストックサービス・自社撮影・ライセンスが明示された素材など、
利用許諾が確認できるものだけを使うこと。本リポジトリは素材の取得手段を提供しない。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


def _load_channel_config(channel: str | None) -> dict:
    from channel_resolver import channel_config  # noqa: E402
    try:
        cfg = channel_config(channel) or {}
    except Exception:
        cfg = {}
    return cfg


def _max_per_video(cfg: dict) -> int:
    """channel.yaml の content.ai_image.max_per_video を返す。未設定なら 10。"""
    try:
        return int(cfg.get("content", {}).get("ai_image", {}).get("max_per_video", 10))
    except (TypeError, ValueError):
        return 10


def _classify_source(sid: str, meta: dict) -> str:
    """ソースを A / AI / C / other に分類して返す。

    A   : kind=video (動画素材。ライセンスが明示されたもの)
    AI  : kind=image かつ (id が ai_/AI プレフィックス or ai_generated=true)
    C   : kind=image でAIでないもの (Commons / Wikipedia等)
    other: 未知 kind (異常検知用)
    """
    kind = meta.get("kind", "video")
    if kind == "video":
        return "A"
    if kind == "image":
        if sid.startswith("ai_") or sid.startswith("AI"):
            return "AI"
        if meta.get("ai_generated", False):
            return "AI"
        return "C"
    return "other"


def check(topic: str, channel: str | None = None) -> int:
    """scenes.json を検証し、問題があれば非ゼロを返す。

    Returns:
        0: PASS (WARNのみも 0)
        1: BLOCK
    """
    allow_ai_heavy = os.environ.get("ALLOW_AI_HEAVY", "").strip() == "1"

    # --- scenes.json 解決 ---
    from channel_resolver import videos_root  # noqa: E402
    base = videos_root(channel) / topic
    scenes_path = base / "scenes.json"
    if not scenes_path.exists():
        print(f"[SKIP] scenes.json が見つかりません: {scenes_path}", flush=True)
        return 0  # 未生成なら未達ではないのでスキップ

    data = json.loads(scenes_path.read_text())
    _raw_sources = data.get("sources", {})
    # sources は dict 形式({id: meta}) または list 形式([{id:..., ...}]) 両対応
    if isinstance(_raw_sources, list):
        sources = {item["id"]: {k: v for k, v in item.items() if k != "id"} for item in _raw_sources}
    else:
        sources = _raw_sources

    # --- 集計 ---
    counts: dict[str, int] = {"A": 0, "AI": 0, "C": 0, "other": 0}
    for sid, meta in sources.items():
        c = _classify_source(sid, meta)
        counts[c] += 1

    total = sum(counts.values())
    a_n = counts["A"]
    ai_n = counts["AI"]
    ratio = ai_n / total if total > 0 else 0.0

    # --- channel.yaml から上限取得 ---
    cfg = _load_channel_config(channel)
    max_ai = _max_per_video(cfg)

    blocks: list[str] = []
    warns: list[str] = []

    if a_n == 0:
        blocks.append(
            "[BLOCK] A(動画素材)=0、企画破綻。ライセンスが明示された動画素材を最低1本用意してください"
        )
    if total > 0 and ratio >= 1.0:
        blocks.append("[BLOCK] 全ソースAI画像、企画成立せず")
    if ai_n > max_ai:
        blocks.append(
            f"[BLOCK] AI画像{ai_n}枚 > max_per_video={max_ai} "
            f"(channel.yaml content.ai_image.max_per_video)"
        )
    if total > 0 and ratio > 0.6:
        warns.append(f"[WARN] AI画像比率{ratio:.0%}、視聴感が単調になる懸念")

    # --- 結果出力 ---
    label = f"qa_source_minimums [{topic}]"

    for w in warns:
        print(f"{label} {w}", flush=True)

    if blocks:
        if allow_ai_heavy:
            for b in blocks:
                degraded = b.replace("[BLOCK]", "[WARN/ALLOW_AI_HEAVY]")
                print(f"{label} {degraded}", flush=True)
            print(
                f"{label} [WARN] ALLOW_AI_HEAVY=1 でブロックをスキップ "
                f"(A={a_n} AI={ai_n}/{max_ai} ratio={ratio:.0%})",
                flush=True,
            )
            return 0
        else:
            for b in blocks:
                print(f"{label} {b}", flush=True)
            print(
                f"{label} [INFO] A={a_n} AI={ai_n}(max={max_ai}) "
                f"C={counts['C']} total={total} ratio={ratio:.0%}",
                flush=True,
            )
            return 1

    print(
        f"{label} [PASS] sources構成 OK "
        f"(A={a_n} AI={ai_n}(max={max_ai}) C={counts['C']} total={total} ratio={ratio:.0%})",
        flush=True,
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="scenes.json の sources 構成をチェックし、違反があれば exit 1 する"
    )
    ap.add_argument("--topic", required=True, help="動画スラッグ (例: <topic>)")
    ap.add_argument("--channel", default=None, help="チャンネル名 (省略時は .current_channel)")
    args = ap.parse_args()

    rc = check(args.topic, args.channel)
    sys.exit(rc)


if __name__ == "__main__":
    main()

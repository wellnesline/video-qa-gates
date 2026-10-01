#!/usr/bin/env python3.9
"""
qa_shorts_visual_diversity.py
Shorts B-roll 視覚多様性 QA ゲート

運用ルール V11(設計メモ・非同梱)に準拠。

検査ルール:
  R1: 静止画像の使用回数 <= image_max_uses (channel.yaml content.shorts.diversity.image_max_uses, default=1)
      【pair_idx 対応 】: pair_idx フィールドが存在するエントリは同一 pair_idx 内での
      同一 broll_source 共有を許容(ペア2ライン=1枚画像の意図的設計)。
      pair 間(異なる pair_idx)での同一画像流用は V12 で別途検出。
  R2: 動画ソースの使用回数 <= video_max (channel.yaml video_max_override or ceil(n_lines/2))
  R3: (src, scene_time) ペアの重複ゼロ (同一動画の同一タイムスタンプ再利用禁止)
  V8: (廃止 ) 旧「ライン独立画像1枚制限」ルール。pair_idx 導入に伴い R1 へ統合。
      後方互換のため旧形式(pair_idx なし)は従来通り R1 を適用。
  V9: alignment_score ペア単位評価。pair_idx が存在する場合、ペア内2ラインを1ユニットとして
      集計。旧形式(pair_idx なし)は従来通りライン単位評価(grandfather フォールバック)。
      (実装: qa_shorts_rebuild.py check_alignment_scores 参照)
  V12: pair 間同一画像重複チェック。異なる pair_idx 間で同一 broll_source
       が使われた場合に違反を報告(ペア方式での画像多様性保証)。
       pair_idx なし形式では V12 はスキップ(後方互換)。

入力ファイル(優先順):
  1. shorts/<short_id>/broll_diversity_check.json  (scout/select フェーズが生成する中間物)
  2. shorts/<short_id>/broll_selected.json          (P4未実行環境 fallback)

フォーマット(どちらも共通):
  [ { "broll_source": "<path>", "ai_generated": bool, "scene_time": int|null, ... }, ... ]

出力:
  channels/<ch>/videos/<slug>/shorts/<short_id>/qa_visual_diversity.json
  {
    "short_id": "short-A",
    "topic": "...",
    "n_lines": N,
    "image_max_uses": 1,
    "video_max": N,
    "issues": [...],
    "verdict": "PASS" | "WARN" | "FAIL",
    "enforcement": "warn" | "block"
  }

終了コード:
  0: PASS or WARN
  1: FAIL (enforcement=block かつ violations あり)

CLI:
  python src/qa_shorts_visual_diversity.py --topic <slug> --short-id <A|B|C> [--channel <ch>]
  --enforcement block で enforcement 強制 override(テスト用)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import current_channel, videos_root, channel_config, add_channel_arg  # noqa: E402

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DEFAULT_IMAGE_MAX_USES = 1


def _is_image(src: str) -> bool:
    return Path(src).suffix.lower() in IMAGE_EXTS


def load_broll(short_dir: Path) -> tuple[list[dict], str]:
    """broll_diversity_check.json を優先、なければ broll_selected.json から fallback。"""
    diversity_check = short_dir / "broll_diversity_check.json"
    selected = short_dir / "broll_selected.json"

    if diversity_check.exists():
        data = json.loads(diversity_check.read_text())
        return data, str(diversity_check)
    elif selected.exists():
        data = json.loads(selected.read_text())
        return data, f"{selected} (fallback: broll_diversity_check.json 未存在)"
    else:
        raise FileNotFoundError(
            f"broll_diversity_check.json も broll_selected.json も見つかりません: {short_dir}"
        )


def _has_pair_idx(entries: list[dict]) -> bool:
    """エントリに pair_idx または spike_pair フィールドが1件以上存在するか判定。"""
    return any(("pair_idx" in e or "spike_pair" in e) for e in entries)


def _get_pair_key(entry: dict) -> str | None:
    """pair_idx → spike_pair の優先順でペアキーを返す。存在しなければ None。"""
    if "pair_idx" in entry:
        return str(entry["pair_idx"])
    if "spike_pair" in entry:
        return str(entry["spike_pair"])
    return None


def run_checks(
    entries: list[dict],
    image_max_uses: int,
    video_max: int,
) -> list[str]:
    """多様性ルールを検査し、違反メッセージのリストを返す(空=違反なし)。

    pair_idx / spike_pair フィールドが存在する場合(ペア方式):
      - R1: pair 内同一 broll_source の2件目は違反カウントしない(意図的共有)
      - V12: pair 間での同一画像重複を追加検出
    pair_idx フィールドなし(旧形式 / grandfather): 従来通り R1/R2/R3 のみ評価。
    """
    issues: list[str] = []
    use_pair_mode = _has_pair_idx(entries)

    image_counter: Counter[str] = Counter()
    video_counter: Counter[str] = Counter()
    seen_time_pairs: set[tuple[str, int]] = set()

    # pair_mode: pair 内で既に計上済みの (pair_key, src) を記録して2件目をスキップ
    pair_image_seen: set[tuple[str, str]] = set()
    # V12 用: pair_idx をキーに、使用した broll_source(画像)集合を記録
    pair_src_map: dict[str, set[str]] = {}

    for entry in entries:
        src = entry.get("broll_source", "")
        if not src:
            continue

        pair_key = _get_pair_key(entry) if use_pair_mode else None

        if _is_image(src) or entry.get("ai_generated", False):
            # AI生成画像も画像扱い
            if use_pair_mode and pair_key is not None:
                pk_src = (pair_key, src)
                if pk_src not in pair_image_seen:
                    # pair 内の初回使用のみカウント対象
                    image_counter[src] += 1
                    pair_image_seen.add(pk_src)
                # else: 同一 pair 内2回目は R1 カウントしない(R1 pair 内同一許容)

                # V12: pair 間重複チェック用マッピング
                pair_src_map.setdefault(pair_key, set()).add(src)
            else:
                # 旧形式: 従来通り全件カウント
                image_counter[src] += 1
        else:
            video_counter[src] += 1
            st = entry.get("scene_time")
            if st is not None:
                key = (src, int(st))
                if key in seen_time_pairs:
                    issues.append(
                        f"R3: (src, scene_time) 重複 — {Path(src).name} t={st}s"
                    )
                seen_time_pairs.add(key)

    # R1: 画像使用回数(pair_mode では pair 単位にカウント済みの件数で評価)
    for src, count in image_counter.items():
        if count > image_max_uses:
            issues.append(
                f"R1: 画像使用回数超過 — {Path(src).name} {count}回 > max {image_max_uses}回"
            )

    # R2: 動画使用回数
    for src, count in video_counter.items():
        if count > video_max:
            issues.append(
                f"R2: 動画使用回数超過 — {Path(src).name} {count}回 > max {video_max}回"
            )

    # V12: pair 間同一画像重複チェック(pair_mode のみ)
    if use_pair_mode and pair_src_map:
        all_pairs = list(pair_src_map.keys())
        for i in range(len(all_pairs)):
            for j in range(i + 1, len(all_pairs)):
                pk_a = all_pairs[i]
                pk_b = all_pairs[j]
                shared = pair_src_map[pk_a] & pair_src_map[pk_b]
                for shared_src in sorted(shared):
                    issues.append(
                        f"V12: pair 間同一画像重複 — {Path(shared_src).name} "
                        f"(pair {pk_a} と pair {pk_b} で共有)"
                    )

    return issues


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Shorts B-roll 視覚多様性 QA (ルール V11)"
    )
    ap.add_argument("--topic", required=True, help="動画スラグ(例: <topic>)")
    ap.add_argument("--short-id", required=True, help="short識別子(例: short-A)")
    ap.add_argument(
        "--enforcement",
        choices=["warn", "block"],
        default=None,
        help="enforcement を CLI から override(未指定時は channel.yaml 準拠)",
    )
    add_channel_arg(ap)
    args = ap.parse_args()

    ch = current_channel(args.channel)
    topic_dir = videos_root(ch) / args.topic
    short_dir = topic_dir / "shorts" / args.short_id

    if not short_dir.exists():
        print(f"[ERROR] short ディレクトリが見つかりません: {short_dir}", file=sys.stderr)
        sys.exit(1)

    # channel.yaml から diversity 設定を取得
    cfg = channel_config(ch)
    diversity_cfg: dict = (
        cfg.get("content", {}).get("shorts", {}).get("diversity", {})
    )
    image_max_uses: int = diversity_cfg.get("image_max_uses", DEFAULT_IMAGE_MAX_USES)
    video_max_override = diversity_cfg.get("video_max_override")  # None = 動的計算
    enforcement_cfg: str = diversity_cfg.get("enforcement", "warn")

    # --enforcement CLI override
    enforcement: str = args.enforcement if args.enforcement else enforcement_cfg

    # broll ロード(fallback 込み)
    try:
        entries, source_label = load_broll(short_dir)
    except FileNotFoundError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    n_lines = len(entries)

    # video_max 決定
    if video_max_override is not None:
        video_max = int(video_max_override)
    else:
        video_max = math.ceil(n_lines / 2) if n_lines > 0 else 1

    # 検査実行
    issues = run_checks(entries, image_max_uses=image_max_uses, video_max=video_max)

    # 判定
    if not issues:
        verdict = "PASS"
    elif enforcement == "block":
        verdict = "FAIL"
    else:
        verdict = "WARN"

    result = {
        "short_id": args.short_id,
        "topic": args.topic,
        "channel": ch,
        "source": source_label,
        "n_lines": n_lines,
        "image_max_uses": image_max_uses,
        "video_max": video_max,
        "enforcement": enforcement,
        "issues": issues,
        "verdict": verdict,
    }

    # 出力先
    out_path = short_dir / "qa_visual_diversity.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    # 標準出力サマリ
    print("=== qa_shorts_visual_diversity ===")
    print(f"channel    : {ch}")
    print(f"topic      : {args.topic}")
    print(f"short_id   : {args.short_id}")
    print(f"source     : {source_label}")
    print(f"n_lines    : {n_lines}")
    print(f"image_max  : {image_max_uses}")
    print(f"video_max  : {video_max}  (override={video_max_override})")
    print(f"enforcement: {enforcement}")
    print(f"issues     : {len(issues)}")
    for iss in issues:
        print(f"  - {iss}")
    print(f"verdict    : {verdict}")
    print(f"output     : {out_path}")

    if verdict == "FAIL":
        sys.exit(1)


if __name__ == "__main__":
    main()

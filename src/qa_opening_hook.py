"""冒頭10秒のpeak hook検証(av_only or final どちらでも可)。

content-scout が sources_candidates.md に記載した peak_timecode を、
scriptwriter が L1〜L3 のいずれかに転記しているかを機械チェック。

また sources_candidates.md に記載のない source_id を script.json の
冒頭ラインが参照している場合(ghost_sources)を検出し FAIL とする。

これは final ビルド **前** の hard gate。
peak_timecode 未使用 or ghost_sources 検出なら build をブロックして修正を強制。
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import videos_root, add_channel_arg  # noqa: E402


def extract_peak_timecodes(candidates_md: Path) -> dict[str, dict]:
    """sources_candidates.md から peak_timecode 行を抽出。

    テーブル形式:
      | id | ファイル | ... | peak_timecode | ... |
      | vid_foo | ... | 2s (closeup) | ... |

    セクション形式(旧形式、フォールバック):
      ### A-1
      peak_timecode: 18
    """
    if not candidates_md.exists():
        return {}
    text = candidates_md.read_text()
    peaks: dict = {}

    # --- テーブル形式パース ---
    # ヘッダ行で "peak_timecode" 列インデックスを特定
    table_found = False
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]  # 前後の空文字除去
        if "peak_timecode" in cells and "id" in cells:
            # ヘッダ行発見: インデックスを記録
            id_idx = cells.index("id")
            pk_idx = cells.index("peak_timecode")
            table_found = True
            continue
        if table_found:
            # 区切り行(---) はスキップ
            if re.match(r"^[-| ]+$", line):
                continue
            # データ行
            if "|" in line:
                row = [c.strip() for c in line.split("|")]
                row = [c for c in row if c != ""]
                if len(row) <= max(id_idx, pk_idx):
                    continue
                src_id = row[id_idx]
                pk_raw = row[pk_idx]
                # "2s (note)" → 2.0, "18" → 18.0 のように先頭数値を抽出
                m_num = re.match(r"(\d+(?:\.\d+)?)", pk_raw)
                if src_id and m_num:
                    peaks[src_id] = {
                        "peak_timecode": float(m_num.group(1)),
                        "peak_duration": 3.0,  # テーブル形式では peak_duration 省略
                    }

    if peaks:
        return peaks

    # --- セクション形式フォールバック(旧形式) ---
    blocks = re.split(r"\n###\s+", text)
    for blk in blocks:
        m_id = re.search(r"([A-C]-\d+)\b", blk[:80])
        m_pk = re.search(r"peak_timecode\s*[:：]\s*(\d+(?:\.\d+)?)", blk)
        m_pd = re.search(r"peak_duration\s*[:：]\s*(\d+(?:\.\d+)?)", blk)
        if m_id and m_pk:
            peaks[m_id.group(1)] = {
                "peak_timecode": float(m_pk.group(1)),
                "peak_duration": float(m_pd.group(1)) if m_pd else 3.0,
            }
    return peaks


def extract_candidate_source_ids(candidates_md: Path) -> set[str]:
    """sources_candidates.md に記載された全ソース ID を抽出(テーブル形式対応)。

    テーブルの id 列、または ### セクション冒頭の ID を収集し、
    ghost_sources 検出に使う。
    """
    if not candidates_md.exists():
        return set()
    text = candidates_md.read_text()
    ids: set[str] = set()

    # テーブル形式: "id" ヘッダを含む行を探して id 列を抽出
    id_idx: int | None = None
    for line in text.splitlines():
        if "|" not in line:
            id_idx = None
            continue
        cells = [c.strip() for c in line.split("|")]
        cells = [c for c in cells if c]
        if "id" in cells and id_idx is None:
            id_idx = cells.index("id")
            continue
        if id_idx is not None:
            if re.match(r"^[-| ]+$", line):
                continue
            if "|" in line:
                row = [c.strip() for c in line.split("|")]
                row = [c for c in row if c != ""]
                if len(row) > id_idx and row[id_idx]:
                    # "src_a_01 (label)" のような括弧付きを分解
                    raw_id = row[id_idx].split()[0]
                    ids.add(raw_id)

    # セクション形式フォールバック
    if not ids:
        for m in re.finditer(r"###\s+([A-C]-\d+)\b", text):
            ids.add(m.group(1))

    return ids


def scene_time_at_first_10s(script: dict, timings: list[dict] | None = None) -> list[dict]:
    """冒頭10秒に該当するラインを抽出して scene_source/scene_time を返す。"""
    lines = script["lines"]
    cum = 0.0
    res = []
    if timings:
        # timings.json があれば実際の duration を使う
        for i, line in enumerate(lines):
            t = timings[i] if i < len(timings) else line
            dur = float(t.get("duration", 4.5))
            res.append({
                "line_index": i + 1,
                "start": cum,
                "end": cum + dur,
                "scene_source": line.get("scene_source"),
                "scene_time": line.get("scene_time"),
            })
            cum += dur
            if cum >= 10:
                break
    else:
        # timings なしならざっくり 4.5s/line で見積もり
        for i, line in enumerate(lines[:5]):
            dur = 4.5
            res.append({
                "line_index": i + 1,
                "start": cum,
                "end": cum + dur,
                "scene_source": line.get("scene_source"),
                "scene_time": line.get("scene_time"),
            })
            cum += dur
            if cum >= 10:
                break
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    add_channel_arg(ap)
    args = ap.parse_args()

    topic_dir = videos_root(args.channel) / args.topic
    script_path = topic_dir / "script.json"
    scenes_path = topic_dir / "scenes.json"
    candidates_md = topic_dir / "sources_candidates.md"
    timings_path = topic_dir / "work" / "timings.json"

    # --- AI画像100%チャンネル: channel.yaml qa.skip_opening_hook_video_check: true で SKIP ---
    try:
        from channel_resolver import channel_config, current_channel  # noqa: E402
        cfg = channel_config(current_channel(args.channel))
        if cfg.get("qa", {}).get("skip_opening_hook_video_check", False):
            print(
                f"[SKIP] qa_opening_hook: channel '{args.channel}' は AI画像100%のため "
                f"media_kind=video チェックを省略(qa.skip_opening_hook_video_check: true)"
            )
            sys.exit(0)
    except Exception as _e:
        print(f"[WARN] channel_config 読込失敗、skip_opening_hook_video_check は無視: {_e}")

    if not script_path.exists():
        print(f"FAIL: script.json not found: {script_path}", file=sys.stderr)
        sys.exit(2)

    script = json.loads(script_path.read_text())
    timings = json.loads(timings_path.read_text()) if timings_path.exists() else None
    peaks = extract_peak_timecodes(candidates_md)
    candidate_ids = extract_candidate_source_ids(candidates_md)

    opening_lines = scene_time_at_first_10s(script, timings)

    # 冒頭10秒に video kind が含まれているか
    has_video_in_opening = any(
        script["lines"][ln["line_index"] - 1].get("media_kind") == "video"
        for ln in opening_lines
    )

    # peak_timecode が L1-L3 のどれかで scene_time として使われているか
    matched_peak = False
    matched_detail = None
    for ln in opening_lines[:3]:
        sid = ln.get("scene_source", "")
        st = ln.get("scene_time")
        if st is None:
            continue
        for cand_id, pk in peaks.items():
            if abs(float(st) - pk["peak_timecode"]) < 1.0:
                matched_peak = True
                matched_detail = {"line": ln["line_index"], "sid": sid,
                                  "scene_time": st, "candidate_peak": pk["peak_timecode"]}
                break
        if matched_peak:
            break

    # ghost_sources 検出: scenes.json に登録されているが
    # sources_candidates.md に記載のない source_id を冒頭ラインが参照していないか
    ghost_sources: list[dict] = []
    if candidate_ids and scenes_path.exists():
        scenes_data = json.loads(scenes_path.read_text())
        scenes_source_ids = {s["id"] for s in scenes_data.get("sources", [])}
        for ln in opening_lines:
            sid = ln.get("scene_source", "")
            if not sid:
                continue
            # candidates に記載あれば OK
            if sid in candidate_ids:
                continue
            # candidates 未記載かつ scenes.json には存在 → ghost
            if sid in scenes_source_ids:
                ghost_sources.append({
                    "line_index": ln["line_index"],
                    "scene_source": sid,
                    "scene_time": ln.get("scene_time"),
                    "reason": "sources_candidates.md に未記載(ghost source)",
                })

    verdict = "PASS" if (has_video_in_opening and (matched_peak or not peaks) and not ghost_sources) else "FAIL"

    out = {
        "topic": args.topic,
        "opening_lines": opening_lines,
        "has_video_in_opening": has_video_in_opening,
        "peaks_from_candidates": peaks,
        "peak_used_in_first_3_lines": matched_peak,
        "matched_detail": matched_detail,
        "ghost_sources": ghost_sources,
        "verdict": verdict,
    }
    out_path = topic_dir / "output" / "opening_hook_check.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))

    print(f"\n=== 冒頭10秒 hook check ===")
    print(f"verdict: {out['verdict']}")
    print(f"opening lines: {len(opening_lines)}本 / video含む: {has_video_in_opening}")
    print(f"peak候補数: {len(peaks)}, L1-L3 で peak_timecode 使用: {matched_peak}")
    if matched_detail:
        print(f"  matched: {matched_detail}")
    if ghost_sources:
        print(f"ghost_sources 検出: {len(ghost_sources)}件")
        for g in ghost_sources:
            print(f"  L{g['line_index']}: {g['scene_source']} (scene_time={g['scene_time']}) — {g['reason']}")

    if out["verdict"] == "FAIL":
        print("FAIL理由:")
        if not has_video_in_opening:
            print("  - 冒頭10秒に media_kind=video の line がない(静止画ばかりは弱い)")
        if peaks and not matched_peak:
            print(f"  - sources_candidates.md に peak_timecode 定義あるが、L1-L3 の scene_time が使っていない")
            print(f"    候補peaks: {peaks}")
        if ghost_sources:
            print(f"  - 冒頭ラインが sources_candidates.md 未記載の source_id を参照(ghost_sources)")
            for g in ghost_sources:
                print(f"    L{g['line_index']}: {g['scene_source']} → sources_candidates.md への追記 or scene_source 張り直しが必要")
        sys.exit(1)


if __name__ == "__main__":
    main()

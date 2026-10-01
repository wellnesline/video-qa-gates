#!/usr/bin/env python3
"""
qa_primer_shorts.py
短尺(Shorts)動画の QA チェッカー

入力:
  - channels/<ch>/videos/<topic>/shorts/short-A.json
  - channels/<ch>/videos/<topic>/shorts/short-A/final.mp4 (存在する場合)

チェック項目(しきい値・話者・禁止語はすべて設定で渡す。既定値は汎用のサンプル):
  1. duration:  final.mp4 が存在する場合 duration_min-duration_max 秒
  2. lines 数:  lines_min-lines_max 行
  3. 全 line に text / speaker / expression / highlight / telop_top が存在
  4. 禁止語: forbidden_patterns(正規表現のリスト。既定は空)
  5. 「！？」併用: forbid_exclaim_question が true のとき禁止
  6. 禁止部分文字列: forbidden_substrings(既定は空)
  7. speaker は allowed_speakers のいずれか(既定は speaker_a / speaker_b / speaker_c)
  8. diagram id が diagrams/<id>.mp4 に実在するか (null は許容)
  9. 全 line 合計字数: chars_min-chars_max

設定: 環境変数 QA_SHORTS_CONFIG に JSON ファイルのパスを指定する。キーは上記の太字名と
  duration_min / duration_max / lines_min / lines_max / chars_min / chars_max /
  allowed_speakers / forbidden_patterns / forbidden_substrings / forbid_exclaim_question。

CLI:
  python src/qa_primer_shorts.py --topic <slug> [--channel <channel>] [--short-id A]

終了コード:
  0: PASS (または WARN のみ)
  1: FAIL
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import current_channel  # noqa: E402

# ---------------------------------------------------------------------------
# 設定(既定値は汎用のサンプル)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict = {
    "duration_min": 20.0,
    "duration_max": 90.0,
    "lines_min": 5,
    "lines_max": 40,
    "chars_min": 100,
    "chars_max": 800,
    "allowed_speakers": ["speaker_a", "speaker_b", "speaker_c"],
    "forbidden_patterns": [],
    "forbidden_substrings": [],
    "forbid_exclaim_question": False,
}


def load_config(path: str | None = None) -> dict:
    """QA_SHORTS_CONFIG(JSON)を既定値へマージして返す。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    path = path if path is not None else os.environ.get("QA_SHORTS_CONFIG", "")
    if path and Path(path).exists():
        try:
            cfg.update(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


_CFG = load_config()
DURATION_MIN = float(_CFG["duration_min"])
DURATION_MAX = float(_CFG["duration_max"])
LINES_MIN    = int(_CFG["lines_min"])
LINES_MAX    = int(_CFG["lines_max"])
CHARS_MIN    = int(_CFG["chars_min"])
CHARS_MAX    = int(_CFG["chars_max"])

ALLOWED_SPEAKERS = set(_CFG["allowed_speakers"])

# 禁止語(正規表現)。既定は空。分野ごとの禁止表現は設定で渡す。
FORBIDDEN_PATTERNS = list(_CFG["forbidden_patterns"])
FORBIDDEN_SUBSTRINGS = list(_CFG["forbidden_substrings"])

# 「！？」併用パターン(forbid_exclaim_question が true のとき検査)
EXCLAIM_QUESTION_PATTERN = re.compile(r"[！!][？?]|[？?][！!]")
FORBID_EXCLAIM_QUESTION = bool(_CFG["forbid_exclaim_question"])

FFPROBE = os.environ.get("FFPROBE_BIN") or "ffprobe"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def get_video_duration(mp4_path: Path) -> float | None:
    try:
        cmd = [
            FFPROBE, "-hide_banner", "-loglevel", "quiet",
            "-print_format", "json", "-show_streams", str(mp4_path),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return None
        info = json.loads(r.stdout)
        for s in info.get("streams", []):
            d = s.get("duration")
            if d:
                return float(d)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# QA ロジック
# ---------------------------------------------------------------------------

def qa_short(
    short_json_path: Path,
    diagrams_dir: Path,
    final_mp4: Path | None,
) -> tuple[str, list[str]]:
    """QA を実行し (status, messages) を返す。
    status: "PASS" / "WARN" / "FAIL"
    """
    issues: list[str] = []
    warns:  list[str] = []

    # --- JSON 読み込み ---
    if not short_json_path.exists():
        return "FAIL", [f"[FAIL] short JSON not found: {short_json_path}"]

    try:
        data = json.loads(short_json_path.read_text())
    except json.JSONDecodeError as e:
        return "FAIL", [f"[FAIL] JSON parse error: {e}"]

    lines = data.get("lines", [])

    # --- 1. duration チェック ---
    if final_mp4 and final_mp4.exists():
        dur = get_video_duration(final_mp4)
        if dur is None:
            warns.append(f"[WARN] duration check skipped: ffprobe failed on {final_mp4.name}")
        elif dur < DURATION_MIN or dur > DURATION_MAX:
            issues.append(
                f"[FAIL] duration={dur:.1f}s out of range [{DURATION_MIN}-{DURATION_MAX}s]"
            )
        else:
            print(f"  [OK] duration={dur:.1f}s")
    else:
        warns.append("[WARN] final.mp4 not found, duration check skipped")

    # --- 2. lines 数 ---
    n_lines = len(lines)
    if n_lines < LINES_MIN or n_lines > LINES_MAX:
        issues.append(
            f"[FAIL] lines count={n_lines} out of range [{LINES_MIN}-{LINES_MAX}]"
        )
    else:
        print(f"  [OK] lines={n_lines}")

    # --- 3-9. line 毎チェック ---
    total_chars = 0
    required_fields = {"text", "speaker", "expression", "highlight", "telop_top"}

    for i, line in enumerate(lines):
        lid = line.get("id") or f"L{i+1:02d}"

        # 3. 必須フィールド
        for field in required_fields:
            if field not in line or line[field] is None:
                issues.append(f"[FAIL] line {lid}: missing field '{field}'")
            elif field == "text" and str(line[field]).strip() == "":
                issues.append(f"[FAIL] line {lid}: empty 'text'")

        text = str(line.get("text") or "")
        total_chars += len(text)

        # 4. 禁止語(設定で渡した正規表現)
        for pat in FORBIDDEN_PATTERNS:
            if re.search(pat, text):
                issues.append(f"[FAIL] line {lid}: forbidden pattern={pat!r} text={text[:30]!r}")
                break

        # 5. 「！？」併用
        if FORBID_EXCLAIM_QUESTION and EXCLAIM_QUESTION_PATTERN.search(text):
            issues.append(f"[FAIL] line {lid}: 「！？」併用禁止: {text[:40]!r}")

        # 6. 禁止部分文字列(設定で渡す)
        for sub in FORBIDDEN_SUBSTRINGS:
            if sub and sub in text:
                issues.append(f"[FAIL] line {lid}: forbidden substring={sub!r}: {text[:40]!r}")

        # 7. speaker 種別
        sp = line.get("speaker", "")
        if sp not in ALLOWED_SPEAKERS:
            issues.append(f"[FAIL] line {lid}: invalid speaker={sp!r} (allowed: {sorted(ALLOWED_SPEAKERS)})")

        # 8. diagram id 実在チェック
        diag_id = line.get("diagram")
        if diag_id is not None:
            diag_mp4 = diagrams_dir / f"{diag_id}.mp4"
            if not diag_mp4.exists():
                issues.append(
                    f"[FAIL] line {lid}: diagram '{diag_id}.mp4' not found in {diagrams_dir}"
                )

    # --- 9. 全 line 合計字数 ---
    if total_chars < CHARS_MIN or total_chars > CHARS_MAX:
        issues.append(
            f"[FAIL] total chars={total_chars} out of range [{CHARS_MIN}-{CHARS_MAX}]"
        )
    else:
        print(f"  [OK] total_chars={total_chars}")

    # --- 結果判定 ---
    all_messages: list[str] = warns + issues
    if issues:
        return "FAIL", all_messages
    if warns:
        return "WARN", all_messages
    return "PASS", all_messages


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Shorts QA チェッカー")
    ap.add_argument("--topic",    required=True, help="動画 slug (例: <topic>)")
    ap.add_argument("--short-id", default="A",  choices=["A", "B", "C"])
    ap.add_argument("--channel",  default=None, help="チャンネル名 (省略時: .current_channel)")
    ap.add_argument("--no-mp4",   action="store_true", help="final.mp4 の duration チェックをスキップ")
    args = ap.parse_args()

    ch = current_channel(args.channel)
    slug = args.topic
    short_key = f"short-{args.short_id}"

    video_dir    = ROOT / "channels" / ch / "videos" / slug
    shorts_dir   = video_dir / "shorts"
    short_json   = shorts_dir / f"{short_key}.json"
    diagrams_dir = video_dir / "diagrams"
    final_mp4    = shorts_dir / short_key / "final.mp4"

    if args.no_mp4:
        final_mp4 = None

    print(f"QA: {short_json}")
    status, messages = qa_short(short_json, diagrams_dir, final_mp4)

    for msg in messages:
        print(f"  {msg}")

    print(f"\n== QA result: {status} ==")
    if status == "FAIL":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

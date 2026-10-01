"""qa_post_video.py F9-c 装飾記号 BLOCK unit test。

★ → 「カー」 と誤読される問題の検出。
★ ☆ ◆ ◇ ◎ ● が speech に含まれる場合 BLOCK。
同記号 2 連(★★ 等)は匿名化扱いで pass。
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.post_video.qa_post_video import F9_DECORATIVE_CHARS


def _check(speech: str) -> list[str]:
    """F9-c ロジックを再現してブロック一覧を返す。"""
    blocks = []
    for ch in F9_DECORATIVE_CHARS:
        if ch in speech and (ch + ch) not in speech:
            blocks.append(ch)
    return blocks


# ---- true positive ----

def test_star_detected():
    """★ が speech にある場合 BLOCK。"""
    assert _check("★今日のポイント")


def test_all_decorative_detected():
    """全装飾記号が検出される。"""
    for ch in F9_DECORATIVE_CHARS:
        s = f"これは{ch}テストです。"
        result = _check(s)
        assert ch in result, f"装飾記号「{ch}」が検出されなかった"


# ---- 2 連は pass ----

def test_double_star_pass():
    """★★(2 連)は匿名化扱いで pass。"""
    assert not _check("ある★★さんは近所の住人です。")


def test_all_double_pass():
    """全記号の 2 連は pass。"""
    for ch in F9_DECORATIVE_CHARS:
        s = f"これは{ch}{ch}という名前の人物です。"
        result = _check(s)
        assert ch not in result, f"2 連記号「{ch}{ch}」が誤 BLOCK された"


# ---- false positive 防止 ----

def test_no_decorative_pass():
    """装飾記号がない通常の speech は BLOCK しない。"""
    cases = [
        "今日のポイントは3つです。",
        "朝の散歩で見つけた小さな花について説明します。",
        "週520個の荷物を運んでいます。",
    ]
    for s in cases:
        assert not _check(s), f"false positive: {s}"


def test_bullet_circle_not_in_list():
    """■ □ ○ は装飾記号リストに含まれていない → BLOCK しない。"""
    for ch in ["■", "□", "○"]:
        assert ch not in F9_DECORATIVE_CHARS, f"「{ch}」 が F9_DECORATIVE_CHARS に含まれている"


if __name__ == "__main__":
    import traceback
    tests = [
        test_star_detected,
        test_all_decorative_detected,
        test_double_star_pass,
        test_all_double_pass,
        test_no_decorative_pass,
        test_bullet_circle_not_in_list,
    ]
    n_pass = n_fail = 0
    for f in tests:
        try:
            f()
            print(f"  [PASS] {f.__name__}")
            n_pass += 1
        except Exception as e:
            print(f"  [FAIL] {f.__name__}: {e}")
            traceback.print_exc()
            n_fail += 1
    print(f"\n=== {n_pass} pass / {n_fail} fail ===")
    sys.exit(0 if n_fail == 0 else 1)

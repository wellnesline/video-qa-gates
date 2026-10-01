r"""qa_post_video.py F9-d 小数点 + 行末「。」 隣接 WARN unit test。

小数点 (4.0 等) が「ドット」と読まれ decimal-mode が波及する問題の検出。
正規表現: r"\d+\.\d+[^。!?]{0,3}。\s*$" (距離 ≤ 3、 別 PR で BLOCK 昇格予定)
- 本 PR では WARN 降格(実データで false positive が残存したため)
- 4.0 → 距離 ≤ 3 内で WARN 発火を確認
- 「30秒から7.25秒」 → 行末「。」 から 10 字以上離れていれば PASS
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.post_video.qa_post_video import F9_DECIMAL_TAIL_RE


# ---- 正規表現検出確認(距離 ≤ 3、 WARN 降格) ----

def test_decimal_detected():
    """4.0 + 行末「。」 (距離 1) は検出される。"""
    s = "倍率は4.0パーセントです。"
    # 「4.0パーセント」+ 「。」 → 距離 > 3 → PASS (false positive 防止済み)
    # 直接隣接ケースをテスト
    s_direct = "比率は4.0です。"
    assert F9_DECIMAL_TAIL_RE.search(s_direct), f"4.0 直近「。」 が検出されなかった: {s_direct}"


def test_decimal_directly_before_period():
    """小数点直後に 3 文字以内の語 + 「。」 → 検出。"""
    s = "比率は1.2。"
    assert F9_DECIMAL_TAIL_RE.search(s), f"1.2 直前「。」 が検出されなかった: {s}"


def test_decimal_two_char_before_period():
    """小数 + 2 文字 + 「。」 (距離 2) → 検出。"""
    s = "係数は2.75です。"
    # 「です」= 2文字 ≤ 3 → 検出される
    assert F9_DECIMAL_TAIL_RE.search(s), f"距離 2 が検出されなかった: {s}"


# ---- false positive 防止 ----

def test_long_tail_not_detected():
    """「30秒から7.25秒」 + 行末まで距離 > 3 → PASS。"""
    s = "待ち時間は30秒から7.25秒になりましたが、この後さらなる調整も予定されています"
    assert not F9_DECIMAL_TAIL_RE.search(s), f"false positive: {s}"


def test_decimal_far_from_period():
    """小数点から行末「。」 まで 4 字以上 → PASS。"""
    s = "6.3パーセント増加です。"  # 「パーセント削減」= 7文字 > 3
    assert not F9_DECIMAL_TAIL_RE.search(s), f"false positive(距離 > 3): {s}"


def test_no_decimal_pass():
    """小数点がない通常テキスト → PASS。"""
    cases = [
        "今日の話題は2つです。",
        "週520個の荷物を運んでいます。",
        "割合は8パーセントです。",
    ]
    for s in cases:
        assert not F9_DECIMAL_TAIL_RE.search(s), f"false positive(小数なし): {s}"


def test_decimal_not_at_line_end():
    """小数点が行末でなく中間にある → PASS(行末に「。」 なし)。"""
    s = "2.5倍の大きさになりました"
    assert not F9_DECIMAL_TAIL_RE.search(s), f"false positive(行末「。」 なし): {s}"


if __name__ == "__main__":
    import traceback
    tests = [
        test_decimal_detected,
        test_decimal_directly_before_period,
        test_decimal_two_char_before_period,
        test_long_tail_not_detected,
        test_decimal_far_from_period,
        test_no_decimal_pass,
        test_decimal_not_at_line_end,
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

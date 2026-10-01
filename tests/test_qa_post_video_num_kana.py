"""qa_post_video.py F5 数字 kana 検出 unit test。

想定する問題:
- 数字を kana 化すると助詞などと誤認識される(例: 合成サンプルの「ごひゃくななじゅっこ」型の表記)→ BLOCK 化

ルール: 数字は漢字混じり原文のまま、 kana 化禁止
        出典: 設計メモ(非同梱)
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.post_video.qa_post_video import NUM_KANA_RE


def test_detect_classic_kana():
    """数字の kana 化例を検出する。"""
    cases = [
        "週にごひゃくななじゅっこの荷物を運びます。",
        "せんにひゃくさんじゅうえんです。",
        "ろくじゅうきゅうかいめの挑戦。",
    ]
    for s in cases:
        m = NUM_KANA_RE.search(s)
        assert m is not None, f"should detect kana number in: {s}"


def test_no_false_positive_normal_text():
    """通常の日本語(数字 kana 化なし)は検出しない。"""
    cases = [
        "窓の外で、3羽の鳥が鳴いていました。",
        "それは意外でしたね。",
        "続きはまた今度お話しします。",
        "お茶をどうぞ。",
        "週520個の荷物を運びます。",  # 数字漢字、 OK
        "1年換算で12か月分。",
        "先に結論を言います。",
        "今日の話題は2つです。",
        "1つ目、道具の選び方。",
    ]
    for s in cases:
        m = NUM_KANA_RE.search(s)
        assert m is None, f"false positive: {s}\n  detected: {m.group() if m else None}"


def test_ichiban_not_detected():
    """「いちばん」(一番)は単独だと検出しない(連続 3 つ未満)。

    ただし「いちばんやってはいけない」のような流れだと「いち」 + 「ばん」(辞書外)+ 「や」 = 1 トークンだけなので検出されない。
    """
    cases = [
        "いちばん大事だと思っています。",
        "ここが今日いちばん大事です。",
    ]
    for s in cases:
        m = NUM_KANA_RE.search(s)
        # 「いち」が 1 つだけなので連続条件を満たさない → 検出されない
        # ただしこの test 自体が将来 NUM_KANA_TOKENS 拡張で false positive 化する可能性
        assert m is None, f"unexpected detection: {s} → {m.group() if m else None}"


def test_short_kana_number():
    """短い kana 数字(2 連)は検出されない=許容。

    「にじゅっぷん」(20分)等、 短い kana は他の通常日本語と区別困難なので検出スコープ外。
    """
    cases = [
        "にじゅっぷん",  # 「に」「じゅっ」 = 2 連、 検出しない
    ]
    for s in cases:
        m = NUM_KANA_RE.search(s)
        # 2 連だと検出されない(連続 3 つ以上が条件)
        assert m is None, f"unexpected: {s} → {m.group() if m else None}"


if __name__ == "__main__":
    test_funcs = [
        test_detect_classic_kana,
        test_no_false_positive_normal_text,
        test_ichiban_not_detected,
        test_short_kana_number,
    ]
    n_pass = 0
    n_fail = 0
    for f in test_funcs:
        try:
            f()
            print(f"  [PASS] {f.__name__}")
            n_pass += 1
        except AssertionError as e:
            print(f"  [FAIL] {f.__name__}: {e}")
            n_fail += 1
    print(f"\n=== {n_pass} pass / {n_fail} fail ===")
    sys.exit(0 if n_fail == 0 else 1)

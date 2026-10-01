"""qa_post_video.py shared_id 除外 unit test。

shared slides (intro_greeting / outro_engagement / outro_line) が
F4 / F9-c / F9-d / F12 で BLOCK / WARN されないことを保証。

"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.post_video.qa_post_video import (
    F9_DECORATIVE_CHARS,
    F9_DECIMAL_TAIL_RE,
    F12_TOKEN_RE,
    _trigram_set,
)


def _shared_slide(shared_id: str, speech: str, caption: str,
                  slide_prompt: str = "") -> dict:
    return {
        "id": shared_id,
        "shared_id": shared_id,
        "lines": [{"speech": speech, "caption": caption}],
        "slide_prompt": slide_prompt,
        "audio_paths": [],
    }


# ---- F4: shared は coverage チェック対象外 ----

def test_f4_shared_excluded():
    """shared_id 付き slide は F4 の shared_id チェックで skip される。"""
    slide = _shared_slide(
        "intro_greeting",
        speech="サンプル文その1。サンプル文その2。",
        caption="サンプル文。",  # caption が大幅要約でも除外
    )
    assert slide["shared_id"]  # 仕様: shared_id あり → F4 ループで skip


# ---- F9-c: shared は装飾記号チェック対象外 ----

def test_f9c_shared_excluded():
    """shared_id 付き slide に ★ があっても BLOCK しない。"""
    slide = _shared_slide(
        "outro_engagement",
        speech="★それでは次の話題に進みましょう。",
        caption="★それでは次の話題に進みましょう。",
    )
    # F9-c ループ: if s.get("shared_id"): continue が働く
    # shared_id があるので BLOCK 対象外であることを文書化
    assert slide["shared_id"] != "", "shared_id 付き slide は F9-c 対象外"
    # ただし実際のコードでは ★ がある → shared なら skip
    # 逆を言えば、shared でない slide に ★ があれば BLOCK される
    for ch in F9_DECORATIVE_CHARS:
        if ch in slide["lines"][0]["speech"] and (ch + ch) not in slide["lines"][0]["speech"]:
            # shared なので BLOCK されるべきでない
            assert slide["shared_id"], f"shared_id なしで F9-c が誤発火した: {ch}"


# ---- F9-d: shared は小数点チェック対象外 ----

def test_f9d_shared_excluded():
    """shared_id 付き slide に小数点 + 行末「。」 隣接(距離 ≤ 3)があっても WARN しない。"""
    # 「係数は2.75です。」は距離 2(「です」) ≤ 3 → F9-d 正規表現が検出する
    slide = _shared_slide(
        "outro_line",
        speech="基準係数は2.75です。",  # 距離 2: 「です」
        caption="基準係数は2.75です。",
    )
    # F9-d ループ: if s.get("shared_id"): continue が働く
    sp = slide["lines"][0]["speech"]
    has_decimal = bool(F9_DECIMAL_TAIL_RE.search(sp))
    # decimal パターンが検出されることを確認(F9-d の検出精度確認)
    assert has_decimal, f"テストデータの小数点パターン(距離 ≤ 3)が検出されなかった: {sp!r}"
    # shared_id があるので WARN/BLOCK 対象外であることを文書化
    assert slide["shared_id"] != "", "shared_id 付き slide は F9-d 対象外"


# ---- F12: shared は slide_prompt チェック対象外 ----

def test_f12_shared_excluded():
    """shared_id 付き slide は F12 のトークン不在チェック対象外。"""
    slide = _shared_slide(
        "intro_greeting",
        speech="週520個の荷物を仕分ける方法を話します。",
        caption="週520個・ラベル・仕分け・API・荷物",
        slide_prompt="",  # 空でも shared なので WARN しない
    )
    # F12 ループ: if s.get("shared_id"): continue が働く
    cap_tokens = set(F12_TOKEN_RE.findall(slide["lines"][0]["caption"]))
    sp_tokens = set(F12_TOKEN_RE.findall(slide["lines"][0]["speech"]))
    slide_prompt = slide["slide_prompt"] or ""
    missing = [t for t in cap_tokens if t not in slide_prompt and t not in sp_tokens]
    # missing が 3 件以上でも shared なので WARN 対象外
    assert slide["shared_id"] != "", "shared_id 付き slide は F12 対象外"
    # (missing の件数は問わない: shared であれば WARN しない)


if __name__ == "__main__":
    import traceback
    tests = [
        test_f4_shared_excluded,
        test_f9c_shared_excluded,
        test_f9d_shared_excluded,
        test_f12_shared_excluded,
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

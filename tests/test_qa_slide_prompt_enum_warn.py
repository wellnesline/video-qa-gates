"""qa_post_video.py F12 slide_prompt 列挙整合 WARN unit test。

slide_prompt に登場人物の列挙が欠けるケース (画像生成のハルシネーション) の検出。
caption の主要語(3 字以上 カタカナ/ひらがな/英字/数値)が slide_prompt に不在 ≥ 3 件で WARN。
alert fatigue 防止のため閾値 ≥ 3 件。
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.post_video.qa_post_video import F12_TOKEN_RE


def _check_f12(caption: str, speech: str, slide_prompt: str) -> list[str]:
    """F12 ロジックを再現して missing トークン一覧を返す。"""
    sp_tokens = set(F12_TOKEN_RE.findall(speech))
    cap_tokens = set(F12_TOKEN_RE.findall(caption))
    return [t for t in cap_tokens if t not in slide_prompt and t not in sp_tokens]


# ---- true positive ----

def test_three_missing_triggers_warn():
    """caption に 3 件以上のカタカナ/英字主要語が slide_prompt にも speech にも不在 → WARN 相当。"""
    # F12_TOKEN_RE はカタカナ3字以上・英字2字以上・数値を抽出(漢字は対象外)
    # caption にカタカナ職業名 3 件以上 + speech に職種なし + slide_prompt に職種なし → WARN
    caption = "ピアニスト、カメラマン、パティシエ、バリスタが登場します。"
    speech = "今日は4名が登場します。"  # カタカナ職業名なし
    slide_prompt = "白背景、4人の人物"  # カタカナ職業名なし
    missing = _check_f12(caption, speech, slide_prompt)
    assert len(missing) >= 3, f"3 件未満しか検出されなかった: {missing}"


def test_persona_hallucination_case():
    """ハルシネーション再現: slide_prompt に職業名が列挙されていない。"""
    # speech もシンプルな表現で職業名カタカナを含めない場合 → F12 発火
    caption = "ピアニスト、カメラマン、パティシエが登場します。30万円の企画。"
    speech = "3名の登場人物と予算規模をご紹介します。"  # 職業名カタカナ・数値なし
    slide_prompt = "3人の人物、白背景"  # 職業名・数値未列挙
    missing = _check_f12(caption, speech, slide_prompt)
    assert len(missing) >= 3, f"職業名ハルシネケースで WARN 未発火: {missing}"


# ---- false positive 防止 ----

def test_two_missing_no_warn():
    """不在が 2 件以下は WARN しない(alert fatigue 防止)。"""
    caption = "田中さんと佐藤さん"
    speech = caption
    slide_prompt = "白背景、2人の人物"
    missing = _check_f12(caption, speech, slide_prompt)
    assert len(missing) < 3, f"2 件以下なのに WARN 発火: {missing}"


def test_speech_coverage_excludes_warn():
    """speech に書いてある語は slide_prompt 不在でも missing にカウントしない。"""
    caption = "ピアニスト、カメラマン、パティシエ"
    speech = "ピアニスト、カメラマン、パティシエの3名が登場します。"
    slide_prompt = "白背景、3色"
    # speech に全員含まれるので missing = 0
    missing = _check_f12(caption, speech, slide_prompt)
    assert len(missing) < 3, f"speech にある語が missing に含まれた: {missing}"


def test_slide_prompt_contains_tokens():
    """slide_prompt に caption の主要語が全部含まれれば WARN しない。"""
    caption = "田中医師、佐藤エンジニア、鈴木教師"
    speech = ""
    slide_prompt = "中央に田中医師、右に佐藤エンジニア、左に鈴木教師、白背景"
    missing = _check_f12(caption, speech, slide_prompt)
    assert len(missing) < 3, f"slide_prompt に含まれる語が missing に入った: {missing}"


# ---- token regex ----

def test_token_regex_katakana():
    """カタカナ 3 字以上をトークンとして抽出。"""
    tokens = set(F12_TOKEN_RE.findall("エンジニアとプログラマー"))
    assert "エンジニア" in tokens or any("エンジニア" in t for t in tokens), \
        f"カタカナトークン未検出: {tokens}"


def test_token_regex_english():
    """英字 2 字以上をトークンとして抽出。"""
    tokens = set(F12_TOKEN_RE.findall("API と SDK を使います。"))
    assert any(t in {"API", "SDK"} for t in tokens), f"英字トークン未検出: {tokens}"


def test_token_regex_number():
    """数値をトークンとして抽出。"""
    tokens = set(F12_TOKEN_RE.findall("週520個の荷物"))
    assert any("520" in t for t in tokens), f"数値トークン未検出: {tokens}"


if __name__ == "__main__":
    import traceback
    tests = [
        test_three_missing_triggers_warn,
        test_persona_hallucination_case,
        test_two_missing_no_warn,
        test_speech_coverage_excludes_warn,
        test_slide_prompt_contains_tokens,
        test_token_regex_katakana,
        test_token_regex_english,
        test_token_regex_number,
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

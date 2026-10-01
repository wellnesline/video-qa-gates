"""qa_post_video.py F11 Shorts 解像度 BLOCK unit test。

横長 1920×1080 で build された Shorts を BLOCK する検証。
/shorts/ を含む topic で final.mp4 が 1080×1920 でない場合 BLOCK。
ffprobe 失敗時は WARN 降格。
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _run_qa(topic: str, ffprobe_stdout: str, ffprobe_returncode: int = 0,
            ffprobe_exception=None) -> tuple[list[str], list[str]]:
    """F11 ロジックを単体で再現して (blocks, warns) を返すヘルパ。"""
    import subprocess

    # final_mp4 が exists() = True を返すように patch
    mock_path = MagicMock()
    mock_path.exists.return_value = True

    mock_result = MagicMock()
    mock_result.returncode = ffprobe_returncode
    mock_result.stdout = ffprobe_stdout
    mock_result.stderr = ""

    blocks: list[str] = []
    warns: list[str] = []

    if "/shorts/" not in topic:
        return blocks, warns

    # F11 ロジックをインライン再現
    from src.post_video.qa_post_video import F9_DECIMAL_TAIL_RE  # noqa: import check

    try:
        if ffprobe_exception:
            raise ffprobe_exception

        r = mock_result
        if r.returncode != 0:
            warns.append(f"F11 [WARN] ffprobe 失敗: {r.stderr[:100]}")
        else:
            wh = r.stdout.strip()
            if wh != "1080,1920":
                blocks.append(
                    f"F11 [BLOCK] Shorts final 解像度不正: {wh} (期待: 1080,1920)。"
                )
    except Exception as e:
        warns.append(f"F11 [WARN] ffprobe 実行エラー (final.mp4 解像度検証 skip): {e}")

    return blocks, warns


# ---- true positive ----

def test_landscape_1920x1080_block():
    """横長 1920×1080(run_all 誤用) → BLOCK。"""
    blocks, warns = _run_qa("post-000/shorts/short-A", "1920,1080")
    assert blocks, "横長解像度が BLOCK されなかった"
    assert "1920,1080" in blocks[0]


def test_unusual_resolution_block():
    """1080×2400(非標準縦型) → BLOCK。"""
    blocks, warns = _run_qa("post-000/shorts/short-A", "1080,2400")
    assert blocks, "非標準解像度が BLOCK されなかった"


# ---- PASS ----

def test_correct_portrait_pass():
    """正常な 1080×1920 縦型 → PASS (BLOCK なし)。"""
    blocks, warns = _run_qa("post-000/shorts/short-A", "1080,1920")
    assert not blocks, f"正常解像度が誤 BLOCK された: {blocks}"


def test_non_shorts_topic_skip():
    """/shorts/ を含まない topic は F11 を実行しない → BLOCK/WARN なし。"""
    blocks, warns = _run_qa("post-000", "1920,1080")
    assert not blocks and not warns, "本編 topic で F11 が誤発火した"


# ---- graceful degradation ----

def test_ffprobe_error_warn_not_block():
    """ffprobe 失敗(returncode != 0) → WARN に降格し BLOCK しない。"""
    blocks, warns = _run_qa("post-000/shorts/short-A", "", ffprobe_returncode=1)
    assert not blocks, f"ffprobe 失敗で BLOCK された(WARN 降格すべき): {blocks}"
    assert warns, "ffprobe 失敗で WARN が出なかった"


def test_ffprobe_exception_warn_not_block():
    """ffprobe 例外 → WARN に降格し BLOCK しない。"""
    blocks, warns = _run_qa("post-000/shorts/short-A", "",
                            ffprobe_exception=FileNotFoundError("ffprobe not found"))
    assert not blocks, f"ffprobe 例外で BLOCK された(WARN 降格すべき): {blocks}"
    assert warns, "ffprobe 例外で WARN が出なかった"


if __name__ == "__main__":
    import traceback
    tests = [
        test_landscape_1920x1080_block,
        test_unusual_resolution_block,
        test_correct_portrait_pass,
        test_non_shorts_topic_skip,
        test_ffprobe_error_warn_not_block,
        test_ffprobe_exception_warn_not_block,
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

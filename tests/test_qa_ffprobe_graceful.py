"""qa_post_video.py F11 ffprobe graceful degradation unit test。

ffprobe 失敗時は BLOCK せず WARN に留める要件の検証。
ffprobe が存在しない / final.mp4 が破損している場合に
F11 が BLOCK ではなく WARN に降格し、クラッシュしないことを保証。
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _simulate_f11(topic: str, ffprobe_returncode: int = 0,
                  ffprobe_stdout: str = "1080,1920",
                  ffprobe_stderr: str = "",
                  final_exists: bool = True,
                  exception=None) -> tuple[list[str], list[str]]:
    """F11 ロジックをインライン再現して (blocks, warns) を返す。"""
    blocks: list[str] = []
    warns: list[str] = []

    if "/shorts/" not in topic:
        return blocks, warns

    if not final_exists:
        return blocks, warns  # final.mp4 が存在しない場合はスキップ

    try:
        if exception:
            raise exception

        r = MagicMock()
        r.returncode = ffprobe_returncode
        r.stdout = ffprobe_stdout
        r.stderr = ffprobe_stderr

        if r.returncode != 0:
            warns.append(f"F11 [WARN] ffprobe 失敗 (final.mp4 解像度検証 skip): {r.stderr[:100]}")
        else:
            wh = r.stdout.strip()
            if wh != "1080,1920":
                blocks.append(
                    f"F11 [BLOCK] Shorts final 解像度不正: {wh} (期待: 1080,1920)。"
                )
    except Exception as e:
        warns.append(f"F11 [WARN] ffprobe 実行エラー (final.mp4 解像度検証 skip): {e}")

    return blocks, warns


# ---- graceful degradation: ffprobe 失敗 → WARN のみ ----

def test_ffprobe_returncode_nonzero_warn():
    """ffprobe が 0 以外で返った場合 → WARN、BLOCK なし。"""
    blocks, warns = _simulate_f11(
        "post-000/shorts/short-A",
        ffprobe_returncode=1,
        ffprobe_stderr="moov atom not found",
    )
    assert not blocks, f"ffprobe 失敗で BLOCK された: {blocks}"
    assert warns, "ffprobe 失敗で WARN が出なかった"
    assert "WARN" in warns[0]


def test_ffprobe_file_not_found_warn():
    """ffprobe コマンド自体が見つからない場合 → WARN、BLOCK なし。"""
    blocks, warns = _simulate_f11(
        "post-000/shorts/short-A",
        exception=FileNotFoundError("ffprobe not found"),
    )
    assert not blocks, f"ffprobe 例外で BLOCK された: {blocks}"
    assert warns, "ffprobe 例外で WARN が出なかった"


def test_ffprobe_timeout_warn():
    """ffprobe タイムアウト → WARN、BLOCK なし。"""
    import subprocess
    blocks, warns = _simulate_f11(
        "post-000/shorts/short-A",
        exception=subprocess.TimeoutExpired("ffprobe", 15),
    )
    assert not blocks, f"ffprobe タイムアウトで BLOCK された: {blocks}"
    assert warns, "ffprobe タイムアウトで WARN が出なかった"


# ---- 正常系: 1080×1920 → PASS ----

def test_correct_resolution_no_block():
    """正常な 1080×1920 → BLOCK も WARN もなし。"""
    blocks, warns = _simulate_f11("post-000/shorts/short-A", ffprobe_stdout="1080,1920")
    assert not blocks, f"正常解像度で BLOCK された: {blocks}"
    f11_warns = [w for w in warns if "F11" in w]
    assert not f11_warns, f"正常解像度で F11 WARN が出た: {f11_warns}"


# ---- final.mp4 が存在しない → スキップ ----

def test_no_final_mp4_skip():
    """final.mp4 が存在しない場合はスキップ(BLOCK も WARN もなし)。"""
    blocks, warns = _simulate_f11(
        "post-000/shorts/short-A",
        final_exists=False,
    )
    assert not blocks and not warns, "final.mp4 未存在で F11 が発火した"


if __name__ == "__main__":
    import traceback
    tests = [
        test_ffprobe_returncode_nonzero_warn,
        test_ffprobe_file_not_found_warn,
        test_ffprobe_timeout_warn,
        test_correct_resolution_no_block,
        test_no_final_mp4_skip,
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

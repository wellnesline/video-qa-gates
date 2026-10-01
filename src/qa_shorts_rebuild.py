#!/usr/bin/env python3.9
"""
qa_shorts_rebuild.py
Shorts縦型独立合成版 QA チェッカー

Shorts 縦型合成の運用ルール(設計メモ・非同梱)に準拠。
機械チェック項目:
  M1: 縦型解像度 1080x1920
  M2: 尺 28-40s (目標30-40s ±2s / )
  M3: 字数 推奨240-330字 (全ラインのtext合計)
  M4: 「！？」「・」「次回」禁止ワード検出
  M5: G3-shorts フリーズ検出 2.0s 閾値
  M6: overlay chain 段数上限警告 (間接確認: ライン数チェック)
  M7: BGM存在チェック (audio stream確認)
  M8: audio channels >= 2 (ステレオ確認)

出力: QA結果JSON + PASS/FAIL 終了コード
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import current_channel  # noqa: E402

FFMPEG  = os.environ.get("FFMPEG_BIN") or ((shutil.which("ffmpeg") or "ffmpeg"))
FFPROBE = os.environ.get("FFPROBE_BIN") or ((shutil.which("ffprobe") or "ffprobe"))

# 閾値
EXPECTED_W     = 1080
EXPECTED_H     = 1920
DUR_MIN        = 28.0   # 目標30-40s, ±2s 許容(既定。チャンネルで上書き要時は _resolve_dur_range 経由)
DUR_MAX        = 40.0   # 上振れ余裕広め


def _resolve_dur_range(channel: str | None) -> tuple[float, float]:
    """channel.yaml content.shorts.duration_sec を読んで動的にdur range算出。
    fallback=既定 28.0-40.0(例: 35秒想定)。
    チャンネルごとの override (例: 60秒) に対応。
    """
    if not channel:
        return (DUR_MIN, DUR_MAX)
    try:
        import yaml as _yaml
        cfg_p = ROOT / "channels" / channel / "channel.yaml"
        if not cfg_p.exists():
            return (DUR_MIN, DUR_MAX)
        cfg = _yaml.safe_load(cfg_p.read_text()) or {}
        d = (cfg.get("content") or {}).get("shorts") or {}
        target = float(d.get("duration_sec") or 35)
        return (max(target - 7.0, 5.0), target + 10.0)
    except Exception:
        return (DUR_MIN, DUR_MAX)
CHAR_MIN       = 50     # 字数下限(ライン数次第、機械下限)
CHAR_MAX       = 400    # 字数上限(推奨240-330字)
FREEZE_THRESH  = 2.0    # G3-shorts: 2.0s
# outro CTA カード廃止。freeze検査の outro 除外ロジックも併せて廃止
MAX_LINES      = 15     # overlay chain: ライン数上限(立ち絵+吹き出しで2n+1段)

BANNED_PATTERNS = [
    (r"！？|？！", "「！？」「？！」組み合わせ禁止"),
    (r"・",        "中黒「・」禁止"),
    (r"次回|明日\d+時|明日の\d+時", "次回・放送時刻言及禁止"),
]


def ffprobe_info(video: Path) -> dict:
    cmd = [
        FFPROBE, "-hide_banner", "-loglevel", "quiet",
        "-print_format", "json", "-show_streams", "-show_format",
        str(video),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return {}
    return json.loads(r.stdout)


def detect_freezes(video: Path, noise: float = 0.001, min_dur: float = FREEZE_THRESH) -> list[dict]:
    """ffmpeg freezedetect で固定フレーム区間を検出 (G3-shorts: 2.0s閾値)"""
    r = subprocess.run(
        [
            FFMPEG, "-i", str(video),
            "-vf", f"freezedetect=n={noise}:d={min_dur}",
            "-map", "0:v:0", "-f", "null", "-",
        ],
        capture_output=True, text=True,
    )
    log = r.stderr
    events = []
    for line in log.splitlines():
        if "freeze_start" in line:
            m = re.search(r"freeze_start: ([\d.]+)", line)
            if m:
                events.append({"start": float(m.group(1))})
        elif "freeze_end" in line and events:
            m_end  = re.search(r"freeze_end: ([\d.]+)", line)
            m_dur  = re.search(r"freeze_duration: ([\d.]+)", line)
            if m_end:
                events[-1]["end"] = float(m_end.group(1))
            if m_dur:
                events[-1]["duration"] = float(m_dur.group(1))
    return events


def measure_wav_mean_volume(wav: Path) -> float | None:
    """ffmpeg volumedetect で mean_volume(dBFS) を返す。失敗時 None。"""
    if not wav.exists():
        return None
    r = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", str(wav), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    log = r.stderr or ""
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", log)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _is_silent_wav(wav_path: Path) -> bool:
    """WAV 全 sample が zero 値か。silent placeholder 検出用。"""
    import wave
    try:
        with wave.open(str(wav_path), "rb") as w:
            n = w.getnframes()
            chunk = min(n, 48000 * 30)
            data = w.readframes(chunk)
        return all(b == 0 for b in data) and len(data) > 0
    except Exception:
        return False


def check_voice_silent_and_dup(voice_dir: Path) -> dict:
    """voice/L*.wav 全件で silent placeholder 検出 + 同サイズ多数を suspicious 警告。

    M11(silent): 1 件でも全 sample zero の WAV あれば BLOCK
    M12(dup size): 同サイズ 5 本以上で WARN(R1 BLOCK2 反映: 3→5、極短セリフ誤検出回避)
    """
    wavs = sorted(voice_dir.glob("L*.wav"))
    silent_files = [w.name for w in wavs if _is_silent_wav(w)]
    sizes: dict[int, list[str]] = {}
    for w in wavs:
        sizes.setdefault(w.stat().st_size, []).append(w.name)
    duplicate_size_groups = [
        {"size": sz, "files": files}
        for sz, files in sizes.items() if len(files) >= 5
    ]
    return {
        "silent_files": silent_files,
        "duplicate_size_groups": duplicate_size_groups,
        "n_total": len(wavs),
    }


# M13: VOICEVOX 数字読み hack 検出パターン
# 数字音節語彙限定で false positive 抑制(R1 BLOCK1 反映)。
_DIGIT_READ_RE = (
    r"(?:いち|に|さん|し|ご|ろく|しち|なな|はち|きゅう|く|"
    r"じゅう|ひゃく|せん|まん|おく|ちょう|"
    r"てん|ぱーせんと|えん|どる|あめ[り]かんどる)"
)
HIRAGANA_NUMERAL_PATTERNS = [
    (re.compile(r"ぱーせんと"),
     "ぱーせんと(% の VOICEVOX 用ひらがな・通常文には出ない)"),
    (re.compile(rf"{_DIGIT_READ_RE}[\s ]+{_DIGIT_READ_RE}[\s ]+{_DIGIT_READ_RE}"),
     "VOICEVOX 数字読み hack(3+ 数字音節がスペース区切り)"),
    (re.compile(rf"{_DIGIT_READ_RE}[\s ]*てん[\s ]*{_DIGIT_READ_RE}"),
     "小数点読みパターン(数字+てん+数字)"),
]


def check_diagram_mp4_durations(short_json_path: Path) -> dict:
    """各 diagram mp4 duration が表示窓 (voice 合計) 以上か検証。
    M14: mp4 < 表示窓 で 3秒以降ブラックアウト。
    """
    short_dir = short_json_path.parent / "short-A"
    voice_dir = short_dir / "voice"
    diag_dir = short_dir / "diagrams_fast"
    if not voice_dir.exists() or not diag_dir.exists():
        return {"pass": True, "note": "voice_dir or diag_dir missing, skipped"}
    try:
        data = json.loads(short_json_path.read_text())
    except Exception as e:
        return {"pass": True, "note": f"short_json load fail: {e}"}
    # 各 diagram の表示窓 = voice 合計
    show_windows: dict[str, float] = {}
    for i, ln in enumerate(data.get("lines", [])):
        d = ln.get("diagram")
        if not d:
            continue
        wav = voice_dir / f"L{i + 1:02d}.wav"
        if not wav.exists():
            continue
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(wav)],
            capture_output=True, text=True,
        )
        try:
            dur = float(result.stdout.strip())
        except ValueError:
            dur = 0.0
        show_windows[d] = show_windows.get(d, 0.0) + dur
    # 各 diagram mp4 の duration
    violations = []
    for diag, window in show_windows.items():
        mp4_path = diag_dir / f"{diag}.mp4"
        if not mp4_path.exists():
            violations.append({"diagram": diag, "issue": f"mp4 not found: {mp4_path.name}"})
            continue
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(mp4_path)],
            capture_output=True, text=True,
        )
        try:
            mp4_dur = float(result.stdout.strip())
        except ValueError:
            mp4_dur = 0.0
        if mp4_dur < window - 0.1:
            violations.append({
                "diagram": diag,
                "mp4_dur": round(mp4_dur, 2),
                "show_window": round(window, 2),
                "shortfall": round(window - mp4_dur, 2),
                "issue": f"diagram mp4 ({mp4_dur:.2f}s) < show window ({window:.2f}s)、{window-mp4_dur:.2f}秒 ブラックアウト",
            })
    return {
        "pass": len(violations) == 0,
        "violations": violations,
        "rule": "各 diagram mp4 duration ≥ 表示窓 -0.1s (M14)",
    }


def check_text_hiragana_numerals(lines: list[dict]) -> dict:
    """text にひらがな数字 hack を検出。bubble_text 未設定なら violation。"""
    violations = []
    for i, ln in enumerate(lines, 1):
        text = ln.get("text", "")
        bubble_text = ln.get("bubble_text")
        for pat, reason in HIRAGANA_NUMERAL_PATTERNS:
            if pat.search(text):
                if bubble_text is None:
                    violations.append({
                        "line": i,
                        "text_snippet": text[:60],
                        "pattern_hit": reason,
                        "remedy": "bubble_text フィールドに数字表記版を追加せよ",
                    })
                break
    return {"violations": violations, "n_checked": len(lines)}


def check_voice_balance(voice_dir: Path) -> dict:
    """A1 強化: 各 line wav の mean_volume が全line平均 ±10dB 以内か。"""
    if not voice_dir.exists():
        return {"pass": True, "note": "voice_dir not found, skipped"}
    wavs = sorted(voice_dir.glob("L*.wav"))
    if not wavs:
        return {"pass": True, "note": "no L*.wav, skipped"}
    measurements: list[tuple[str, float]] = []
    for w in wavs:
        v = measure_wav_mean_volume(w)
        if v is not None:
            measurements.append((w.name, v))
    if not measurements:
        return {"pass": False, "note": "volumedetect failed for all wavs"}
    mean = sum(v for _, v in measurements) / len(measurements)
    THRESH = 10.0
    outliers = [
        {"line": name, "mean_volume_dB": round(v, 2), "diff_from_avg_dB": round(v - mean, 2)}
        for name, v in measurements if abs(v - mean) > THRESH
    ]
    return {
        "pass": len(outliers) == 0,
        "avg_mean_volume_dB": round(mean, 2),
        "threshold_dB": THRESH,
        "n_lines": len(measurements),
        "per_line": [{"line": n, "mean_volume_dB": round(v, 2)} for n, v in measurements],
        "outliers": outliers,
    }


def _get_pair_key_rebuild(entry: dict) -> str | None:
    """pair_idx → spike_pair の優先順でペアキーを返す。存在しなければ None。"""
    if "pair_idx" in entry:
        return str(entry["pair_idx"])
    if "spike_pair" in entry:
        return str(entry["spike_pair"])
    return None


def check_alignment_scores(broll_selected_json: Path) -> dict:
    """V9: alignment_score の品質チェック (pair_idx 対応)。

    pair_idx / spike_pair フィールドが存在する場合(ペア方式):
      - ペア単位で alignment_score を集計(同一 pair_idx 内は代表値=最大値を使用)
      - ペア代表スコア≥3 が全ペア 80%以上で PASS
      - 連続 low-score ペアの検出も実施
    pair_idx なし(旧形式 / grandfather):
      - 従来通りライン単位評価(ratio_ge3 >= 0.8 / 連続 low 検出)
    """
    if not broll_selected_json.exists():
        return {"pass": True, "note": "broll_selected.json not found, skipped"}
    try:
        data = json.loads(broll_selected_json.read_text())
    except Exception as e:
        return {"pass": False, "error": str(e)}
    if not isinstance(data, list) or not data:
        return {"pass": True, "note": "empty broll_selected"}

    # pair_idx 有無を判定
    use_pair_mode = any(("pair_idx" in item or "spike_pair" in item) for item in data)

    if use_pair_mode:
        # ペア単位集計: pair_key ごとに最大スコアを代表値として使用
        from collections import OrderedDict
        pair_scores: dict[str, int] = OrderedDict()
        for item in data:
            pk = _get_pair_key_rebuild(item)
            if pk is None:
                # pair_idx なしエントリが混在する場合は individual として扱う
                pk = f"_line_{item.get('line_idx', id(item))}"
            s = int(item.get("alignment_score", 0))
            if pk not in pair_scores:
                pair_scores[pk] = s
            else:
                pair_scores[pk] = max(pair_scores[pk], s)

        scores = list(pair_scores.values())
        pair_keys = list(pair_scores.keys())
        n = len(scores)
        n_ge3 = sum(1 for s in scores if s >= 3)
        ratio = n_ge3 / n if n else 0
        avg = sum(scores) / n if n else 0

        # 連続 low-score ペア検出
        consecutive_low: list[tuple[int, int]] = []
        run_start = None
        for i, s in enumerate(scores):
            if s <= 2:
                if run_start is None:
                    run_start = i
            else:
                if run_start is not None and (i - run_start) >= 2:
                    consecutive_low.append((run_start + 1, i))
                run_start = None
        if run_start is not None and (n - run_start) >= 2:
            consecutive_low.append((run_start + 1, n))

        ratio_ok = ratio >= 0.8
        return {
            "pass": ratio_ok and not consecutive_low,
            "mode": "pair_unit",
            "n_pairs": n,
            "ratio_ge3": round(ratio, 2),
            "ratio_target": 0.8,
            "avg_score": round(avg, 2),
            "pair_scores": dict(zip(pair_keys, scores)),
            "consecutive_low_runs": [{"pair_start": s, "pair_end": e} for s, e in consecutive_low],
        }
    else:
        # 旧形式: ライン単位評価(grandfather フォールバック)
        scores = [int(item.get("alignment_score", 0)) for item in data]
        n = len(scores)
        n_ge3 = sum(1 for s in scores if s >= 3)
        ratio = n_ge3 / n if n else 0
        avg = sum(scores) / n if n else 0
        # 連続 score=2 (rotation) 検出
        consecutive_low_lines: list[tuple[int, int]] = []
        run_start = None
        for i, s in enumerate(scores):
            if s <= 2:
                if run_start is None:
                    run_start = i
            else:
                if run_start is not None and (i - run_start) >= 2:
                    consecutive_low_lines.append((run_start + 1, i))  # 1-indexed
                run_start = None
        if run_start is not None and (n - run_start) >= 2:
            consecutive_low_lines.append((run_start + 1, n))
        ratio_ok = ratio >= 0.8
        return {
            "pass": ratio_ok and not consecutive_low_lines,
            "mode": "line_unit",
            "ratio_ge3": round(ratio, 2),
            "ratio_target": 0.8,
            "avg_score": round(avg, 2),
            "scores": scores,
            "consecutive_low_runs": [{"line_start": s, "line_end": e} for s, e in consecutive_low_lines],
        }


def check_broll_coverage(broll_selected_json: Path) -> dict:
    """M9: broll_selected.json で broll_source が設定されているライン比率 >= 80% を確認。
    broll_source が None のラインが多い = 素材 0% 状態(未DLのまま)で
    合成が進んでしまうことを防ぐ QA ゲート。"""
    if not broll_selected_json.exists():
        return {"pass": True, "note": "broll_selected.json not found, skipped"}
    try:
        data = json.loads(broll_selected_json.read_text())
    except Exception as e:
        return {"pass": False, "error": str(e)}
    if not isinstance(data, list) or not data:
        return {"pass": True, "note": "empty broll_selected, skipped"}
    n = len(data)
    n_covered = sum(1 for item in data if item.get("broll_source") is not None)
    ratio = n_covered / n if n else 0.0
    COVERAGE_MIN = 0.80
    ok = ratio >= COVERAGE_MIN
    return {
        "pass": ok,
        "covered_lines": n_covered,
        "total_lines": n,
        "coverage_ratio": round(ratio, 3),
        "threshold": COVERAGE_MIN,
    }


def check_video(video: Path, short_json: Path | None = None, channel: str | None = None) -> dict:
    """QA全チェック実行。結果dictを返す。
    channel: channel.yaml の duration_sec から dur_range を動的解決(60秒等のch対応)。
    """
    results: dict[str, dict] = {}
    all_pass = True
    dur_min, dur_max = _resolve_dur_range(channel)

    # ffprobe情報取得
    info = ffprobe_info(video)
    streams = info.get("streams", [])
    fmt = info.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    # M1: 縦型解像度
    if video_stream:
        w = video_stream.get("width", 0)
        h = video_stream.get("height", 0)
        ok = (w == EXPECTED_W and h == EXPECTED_H)
        results["M1_resolution"] = {
            "pass": ok,
            "value": f"{w}x{h}",
            "expected": f"{EXPECTED_W}x{EXPECTED_H}",
        }
        if not ok:
            all_pass = False
    else:
        results["M1_resolution"] = {"pass": False, "value": "no_video_stream"}
        all_pass = False

    # M2: 尺チェック(channel.yaml duration_sec 動的解決)
    duration = float(fmt.get("duration", 0))
    ok = dur_min <= duration <= dur_max
    results["M2_duration"] = {
        "pass": ok,
        "value": round(duration, 2),
        "range": f"{dur_min}-{dur_max}s",
    }
    if not ok:
        all_pass = False

    # M3: 字数チェック (short_json がある場合)
    if short_json and short_json.exists():
        try:
            data = json.loads(short_json.read_text())
            total_chars = sum(len(line.get("text", "")) for line in data.get("lines", []))
            ok = CHAR_MIN <= total_chars <= CHAR_MAX
            results["M3_chars"] = {
                "pass": ok,
                "value": total_chars,
                "range": f"{CHAR_MIN}-{CHAR_MAX}",
            }
            if not ok:
                all_pass = False
        except Exception as e:
            results["M3_chars"] = {"pass": False, "error": str(e)}
            all_pass = False

        # M4: 禁止ワード検出
        banned_found = []
        for line in data.get("lines", []):
            text = line.get("text", "")
            for pattern, reason in BANNED_PATTERNS:
                if re.search(pattern, text):
                    banned_found.append({"text_snippet": text[:30], "pattern": pattern, "reason": reason})
        ok = len(banned_found) == 0
        results["M4_banned_words"] = {
            "pass": ok,
            "violations": banned_found,
        }
        if not ok:
            all_pass = False

        # M6: ライン数(overlay chain間接確認)
        n_lines = len(data.get("lines", []))
        # 立ち絵+吹き出しで最大 2n+3段 (hook_top/bot/concat=3)
        estimated_stages = 2 * n_lines + 5
        ok = n_lines <= MAX_LINES and estimated_stages < 50
        results["M6_overlay_chain"] = {
            "pass": ok,
            "lines": n_lines,
            "estimated_stages": estimated_stages,
            "limit": 50,
        }
        if not ok:
            all_pass = False

        # M10: highlight フィールド検証
        # gen_bubbles_shorts.py は highlight 欠落時に silent fallback で強調なし描画する。
        # highlight が全行未設定だとテロップ強調が発火しない。
        # 全行 highlight 必須 + text 含有必須 で BLOCK 化、事前に発覚させる。
        lines_data = data.get("lines", [])
        missing: list[int] = []
        not_in_text: list[dict] = []
        for i, ln in enumerate(lines_data, 1):
            hl = (ln.get("highlight") or "").strip()
            # バグB対応: gen_bubbles_shorts.py が bubble_text を優先描画するため、
            # M10 highlight 含有チェックも bubble_text(なければ text)に対して行う。
            # `is not None` 判定で空文字 "" の bubble_text もそのまま採用。
            bubble_text_raw = ln.get("bubble_text")
            tx = bubble_text_raw if bubble_text_raw is not None else ln.get("text", "")
            if not hl:
                missing.append(i)
            elif hl not in tx:
                not_in_text.append({"line": i, "highlight": hl, "text": tx[:40]})
        ok_m10 = (len(missing) == 0) and (len(not_in_text) == 0)
        results["M10_highlights"] = {
            "pass": ok_m10,
            "n_lines": len(lines_data),
            "missing_lines": missing,
            "not_in_text": not_in_text,
            "rule": "全行 highlight 必須 + text 含有必須 ",
        }
        if not ok_m10:
            all_pass = False

        # M11: silent WAV 検出(BLOCK / バグA 再発防止)
        # M12: WAV size duplicate(WARN / silent fallback の残滓検出補完)
        voice_dir = short_json.parent / "short-A" / "voice"
        if voice_dir.exists():
            vinfo = check_voice_silent_and_dup(voice_dir)
            silent = vinfo["silent_files"]
            dups = vinfo["duplicate_size_groups"]
            results["M11_silent_wav"] = {
                "pass": len(silent) == 0,
                "silent_files": silent,
                "rule": "voice/L*.wav に silent placeholder 検出禁止(bugA 再発防止)",
            }
            if silent:
                all_pass = False  # BLOCK
            results["M12_wav_size_dup"] = {
                "pass": True,  # WARN: 結果記録のみで all_pass に伝播しない(R1 BLOCK2 反映)
                "warn": len(dups) > 0,
                "duplicate_size_groups": dups,
                "rule": "同サイズ WAV 5本以上は silent fallback 疑い(WARN・人間目視推奨)",
            }
        else:
            results["M11_silent_wav"] = {"pass": True, "note": "voice_dir not found, skipped"}
            results["M12_wav_size_dup"] = {"pass": True, "note": "voice_dir not found, skipped"}

        # M13: text ひらがな数字検出(BLOCK / バグB 再発防止)
        m13 = check_text_hiragana_numerals(data.get("lines", []))
        ok_m13 = len(m13["violations"]) == 0
        results["M13_hiragana_numerals"] = {
            "pass": ok_m13,
            "violations": m13["violations"],
            "rule": "text にひらがな数字 hack あれば bubble_text 必須(bugB 再発防止)",
        }
        if not ok_m13:
            all_pass = False  # BLOCK

        # M14: diagram mp4 duration ≥ 表示窓 検証(BLOCK / ブラックアウト防止)
        m14 = check_diagram_mp4_durations(short_json)
        results["M14_diagram_mp4_duration"] = m14
        if not m14.get("pass", True):
            all_pass = False
    else:
        results["M3_chars"] = {"pass": True, "note": "short_json not provided, skipped"}
        results["M4_banned_words"] = {"pass": True, "note": "skipped"}
        results["M6_overlay_chain"] = {"pass": True, "note": "skipped"}
        results["M10_highlights"] = {"pass": True, "note": "short_json not provided, skipped"}
        results["M11_silent_wav"] = {"pass": True, "note": "short_json not provided, skipped"}
        results["M12_wav_size_dup"] = {"pass": True, "note": "short_json not provided, skipped"}
        results["M13_hiragana_numerals"] = {"pass": True, "note": "short_json not provided, skipped"}
        results["M14_diagram_mp4_duration"] = {"pass": True, "note": "short_json not provided, skipped"}

    # M5: G3-shorts フリーズ検出 (2.0s閾値)
    # outro CTA カード廃止に伴い、outro除外ロジックは削除。
    # AI画像はcompose側でKenBurns適用済みのため、原則freeze=0が期待される
    #
    print(f"  [M5] freezedetect (threshold={FREEZE_THRESH}s)...")
    freezes = detect_freezes(video)
    ok = len(freezes) == 0
    results["M5_freeze_G3shorts"] = {
        "pass": ok,
        "threshold": FREEZE_THRESH,
        "freezes": freezes,
        "count": len(freezes),
    }
    if not ok:
        all_pass = False

    # M7: BGM存在チェック (audio stream)
    has_audio = audio_stream is not None
    results["M7_audio_stream"] = {
        "pass": has_audio,
        "codec": audio_stream.get("codec_name", "none") if audio_stream else "none",
    }
    if not has_audio:
        all_pass = False

    # M8: audio channels >= 2
    if audio_stream:
        channels = audio_stream.get("channels", 0)
        ok = channels >= 2
        results["M8_stereo"] = {"pass": ok, "channels": channels}
        if not ok:
            all_pass = False
    else:
        results["M8_stereo"] = {"pass": False, "channels": 0}
        all_pass = False

    return {"video": str(video), "pass": all_pass, "checks": results}


def main():
    ap = argparse.ArgumentParser(description="Shorts縦型独立合成版 QAチェッカー")
    ap.add_argument("--topic",    required=True)
    ap.add_argument("--short-id", required=True, choices=["A", "B", "C"])
    ap.add_argument("--channel",  default=None)
    ap.add_argument("--out",      default=None, help="QA結果JSONパス")
    args = ap.parse_args()

    ch = current_channel(args.channel)
    slug = args.topic
    short_key = f"short-{args.short_id}"

    video_dir  = ROOT / "channels" / ch / "videos" / slug
    build_dir  = video_dir / "shorts" / short_key
    final_mp4  = build_dir / "final.mp4"
    short_json = video_dir / "shorts" / f"{short_key}.json"

    if not final_mp4.exists():
        print(f"[ERROR] final.mp4 not found: {final_mp4}")
        sys.exit(1)

    print(f"[qa_shorts_rebuild] {ch}/{slug}/{short_key}")
    result = check_video(final_mp4, short_json, channel=ch)

    # 追加: A1(voice balance) / V9(alignment scores)
    # M9 追加: broll coverage >= 80% ゲート
    voice_dir = build_dir / "voice"
    broll_sel = build_dir / "broll_selected.json"
    a1 = check_voice_balance(voice_dir)
    v9 = check_alignment_scores(broll_sel)
    m9 = check_broll_coverage(broll_sel)
    result["checks"]["A1_voice_balance"] = a1
    result["checks"]["V9_alignment_scores"] = v9
    result["checks"]["M9_broll_coverage"] = m9
    # A1 は厳格に PASS/FAIL に反映
    if not a1.get("pass", True):
        result["pass"] = False
    # V9 は ratio<0.8 も FAIL 昇格(R3)。pair_mode/line_mode 両対応
    if not v9.get("pass", True):
        result["pass"] = False
        mode_label = v9.get("mode", "line_unit")
        print(f"  [V9 FAIL] alignment ratio_ge3={v9.get('ratio_ge3')} < 0.8 threshold (mode={mode_label})")
    if v9.get("consecutive_low_runs"):
        mode_label = v9.get("mode", "line_unit")
        print(f"  [V9 WARN] alignment_score 連続 low ({mode_label}): {v9['consecutive_low_runs']}")
    # M9 は厳格に PASS/FAIL に反映(素材 0% での合成進行防止)
    if not m9.get("pass", True):
        result["pass"] = False
        print(f"  [M9 FAIL] broll_coverage={m9.get('coverage_ratio', 0):.1%} < {m9.get('threshold', 0.8):.0%} (covered={m9.get('covered_lines', 0)}/{m9.get('total_lines', 0)} lines)")

    # 結果表示
    status = "PASS" if result["pass"] else "FAIL"
    print(f"\n=== QA Result: {status} ===")
    for check_id, check_result in result["checks"].items():
        mark = "OK" if check_result.get("pass") else "FAIL"
        print(f"  [{mark}] {check_id}: {check_result}")

    # JSON保存
    out_path = Path(args.out) if args.out else (build_dir / "qa_result.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nQA result saved: {out_path}")

    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()

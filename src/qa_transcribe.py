"""speech.wav または final.mp4 を文字起こしして script.json と差分比較する QA。

Stage3 ビルド前プリチェックで実行し、VOICEVOX 誤読(イントネーション/別読)を機械的に検知する。
旧版は av_only.mp4 を入力としたが、av_only.mp4 廃止に伴い work/speech.wav(synthesize.py
直後の純粋VOICEVOX出力)を既定入力に変更。
faster-whisper(large-v3 or medium) を CPU/CoreML で回す。

--profile 引数:
  full(デフォルト) = large-v3(高精度、人間提示前 / Stage2 確定 / Stage3 final)
  fast             = medium(高速、rework ループ中の中間確認用)

Stage3 再利用ロジック:
  Stage2 承認後に生成された transcribe_qa.json が存在し、かつ script.json の mtime が
  transcribe_qa.json より新しくない場合は Whisper 再実行をスキップして既存 JSON を採用する。
  (BGM 入り final.mp4 より BGM なし av_only.mp4 のほうが Whisper 精度的に有利なため)
  再利用マトリックス:
    prev=full, curr=full/fast → 再利用(full は精度上位)
    prev=fast, curr=full      → 再実行(精度昇格)
    prev=fast, curr=fast      → 再利用
    prev=None, any            → 再実行(safe-side: 旧 JSON でフィールド欠落)
  --force を渡すと強制的に再実行する。
  出典: 設計メモ(非同梱)の Stage3 transcribe 再利用ルール節
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from channel_resolver import videos_root, add_channel_arg  # noqa: E402


def _get_stage1_approved_at(log_yaml: Path) -> Optional[datetime]:
    """log.yaml から stage1 の approved_at を取得する。

    旧 _get_stage2_approved_at は stage2/stage2_built/stage2_av_only キーを or 検索していたが、
    Stage2 廃止に伴い stage1/stage1_plan の or 判定に変更。

    log.yaml は厳密な YAML でない場合もあるため、簡易テキストマッチで対応する。
    approved_at が見つからない場合は None を返す。
    """
    if not log_yaml.exists():
        return None
    try:
        import yaml  # type: ignore
        data = yaml.safe_load(log_yaml.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # stage1 または stage1_plan キーを探す
        for key in ("stage1", "stage1_plan"):
            block = data.get(key)
            if isinstance(block, dict):
                val = block.get("approved_at")
                if val:
                    if isinstance(val, datetime):
                        return val
                    try:
                        return datetime.fromisoformat(str(val))
                    except ValueError:
                        pass
    except Exception:
        pass
    # yaml モジュール不在 or パース失敗時: テキスト grep で最低限の抽出
    try:
        text = log_yaml.read_text(encoding="utf-8")
        import re as _re
        # "approved_at: '2026-..." or "approved_at: 2026-..." を探す
        m = _re.search(r"approved_at:\s*['\"]?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^'\"\s]*)['\"]?", text)
        if m:
            return datetime.fromisoformat(m.group(1).replace(" ", "T"))
    except Exception:
        pass
    return None


def normalize(s: str) -> str:
    """比較用の正規化: 句読点/空白除去、全角→半角は最低限"""
    s = re.sub(r"[、。！？!?\s「」『』…・,.\-\"'()（）　]", "", s)
    return s


def transcribe(audio_path: Path, profile: str = "full") -> list[tuple[float, float, str]]:
    from faster_whisper import WhisperModel
    # full=large-v3(高精度・~3GB初回DL) / fast=medium(高速・reworkループ用)
    model_name = "medium" if profile == "fast" else "large-v3"
    beam = 1 if profile == "fast" else 5
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        str(audio_path),
        language="ja",
        beam_size=beam,
        vad_filter=True,
        condition_on_previous_text=False,
    )
    return [(seg.start, seg.end, seg.text) for seg in segments]


def line_text_at(script_lines: list[dict], idx: int) -> str:
    return script_lines[idx]["text"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--audio", default=None, help="default: videos/<topic>/work/speech.wav ")
    ap.add_argument("--threshold", type=float, default=0.85,
                    help="文字一致率しきい値(0-1)。これ未満を要確認とする")
    ap.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Stage2 transcribe_qa.json の再利用をスキップして Whisper を強制再実行する",
    )
    ap.add_argument(
        "--profile",
        choices=["full", "fast"],
        default="full",
        help="full=large-v3(高精度) / fast=medium(rework ループ高速化)",
    )
    add_channel_arg(ap)
    args = ap.parse_args()

    topic_dir = videos_root(args.channel) / args.topic
    # Stage2廃止対応: 既定入力を speech.wav に変更
    # fallback として final.mp4 / av_only.mp4 順に存在チェック(過去動画の互換性維持)
    if args.audio:
        audio = Path(args.audio)
    else:
        speech_p = topic_dir / "work" / "speech.wav"
        final_p = topic_dir / "final.mp4"
        av_only_p = topic_dir / "output" / "av_only.mp4"
        if speech_p.exists():
            audio = speech_p
        elif final_p.exists():
            audio = final_p
        else:
            audio = av_only_p  # 過去動画の互換: 存在しなければ後段で FAIL
    script_path = topic_dir / "script.json"
    out_path = topic_dir / "output" / "transcribe_qa.json"

    if not script_path.exists():
        print(f"FAIL: script not found: {script_path}", file=sys.stderr)
        sys.exit(2)

    # ---------------------------------------------------------------
    # 再利用ロジック(--force 未指定時のみ / Stage2廃止対応)
    # 条件:
    #   1. out_path(transcribe_qa.json)が既に存在
    #   2. log.yaml の stage1/stage1_plan.approved_at 後に transcribe_qa.json が生成されている
    #   3. script.json の mtime が transcribe_qa.json 以前(=台本変更なし)
    # ---------------------------------------------------------------
    if (
        not args.force
        and out_path.exists()
    ):
        log_yaml = topic_dir / "log.yaml"
        stage1_approved_at = _get_stage1_approved_at(log_yaml)
        if stage1_approved_at is not None:
            transcribe_mtime = out_path.stat().st_mtime
            script_mtime = script_path.stat().st_mtime
            # stage1_approved_at が naive datetime の場合は UTC として扱う
            if stage1_approved_at.tzinfo is None:
                stage1_ts = stage1_approved_at.replace(tzinfo=timezone.utc).timestamp()
            else:
                stage1_ts = stage1_approved_at.timestamp()

            cond_approved = transcribe_mtime >= stage1_ts
            cond_script_unchanged = script_mtime <= transcribe_mtime

            if cond_approved and cond_script_unchanged:
                # 再利用: reused_from_stage1 キーを追記して即 return
                try:
                    data = json.loads(out_path.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"[qa_transcribe] transcribe_qa.json 読み込み失敗({e})、再実行します", file=sys.stderr)
                    data = None

                if data is not None:
                    # whisper_profile 再利用マトリックス
                    prev_profile = data.get("whisper_profile")  # フィールドなし → None
                    current_profile = args.profile
                    profile_reasons: list[str] = []
                    skip_reuse = False
                    if prev_profile is None:
                        profile_reasons.append(
                            f"whisper_profile フィールドなし(旧JSON) → {current_profile} で再実行"
                        )
                        skip_reuse = True
                    elif prev_profile == "fast" and current_profile == "full":
                        profile_reasons.append(
                            f"prev=fast → curr=full の精度昇格 → 再実行"
                        )
                        skip_reuse = True
                    # prev=full,curr=full / prev=full,curr=fast / prev=fast,curr=fast → 再利用 OK

                    if skip_reuse:
                        print(
                            f"[qa_transcribe] 再利用スキップ({'; '.join(profile_reasons)})、Whisper 再実行します",
                            file=sys.stderr,
                        )
                        data = None  # fall through to re-run

                if data is not None:
                    # 旧 reused_from_stage2 キーは廃止。新規は reused_from_stage1 を使う(後方互換のため両方残す)
                    if not data.get("reused_from_stage1"):
                        data["reused_from_stage1"] = True
                        data["reused_at"] = datetime.now(tz=timezone.utc).isoformat()
                        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(
                        f"[qa_transcribe] transcribe_qa.json を再利用 → "
                        f"{'PASS' if data.get('passed') else 'FAIL'}",
                        file=sys.stderr,
                    )
                    print(f"詳細: {out_path}")
                    sys.exit(0 if data.get("passed") else 1)
            else:
                reasons = []
                if not cond_approved:
                    reasons.append(f"transcribe_mtime({transcribe_mtime:.0f}) < stage1_approved_at({stage1_ts:.0f})")
                if not cond_script_unchanged:
                    reasons.append(f"script.json mtime({script_mtime:.0f}) > transcribe_mtime({transcribe_mtime:.0f}): 台本変更の可能性")
                print(f"[qa_transcribe] 再利用スキップ({'; '.join(reasons)})、Whisper 再実行します", file=sys.stderr)

    if not audio.exists():
        print(f"FAIL: audio not found: {audio}", file=sys.stderr)
        sys.exit(2)
    # 鮮度ゲート: script.json より古い音声に対する transcribe は無意味
    if audio.stat().st_mtime < script_path.stat().st_mtime:
        print(f"[STALE] {audio.name} mtime < script.json mtime → 再 synthesize/compose が必要", file=sys.stderr)
        sys.exit(3)
    print(f"[QA target] {audio} (size={audio.stat().st_size:,}B mtime={int(audio.stat().st_mtime)})")

    script = json.loads(script_path.read_text())
    print(f"transcribing {audio} (profile={args.profile}) ...", flush=True)
    segments = transcribe(audio, profile=args.profile)

    full_transcript = "".join(s[2] for s in segments)
    full_script = "".join(line["text"] for line in script["lines"])

    norm_t = normalize(full_transcript)
    norm_s = normalize(full_script)

    # 1) 全文の文字一致率(set-based ratio)
    from difflib import SequenceMatcher
    ratio = SequenceMatcher(a=norm_s, b=norm_t).ratio()

    # 2) ライン単位の差分検出: scriptの各textが転写中に現れるか
    line_issues = []
    for i, line in enumerate(script["lines"]):
        t = normalize(line["text"])
        if not t:
            continue
        # ライン文字列が転写文に部分一致するかチェック
        # 完全一致は無理なので、前半5文字 or 末尾5文字が現れるかで近似
        head = t[:6]
        tail = t[-6:]
        if head not in norm_t and tail not in norm_t:
            line_issues.append({
                "line_index": i,
                "speaker": line["speaker"],
                "expected": line["text"],
                "head_probe": head,
                "tail_probe": tail,
                "highlight": line.get("highlight"),
            })

    result = {
        "audio": str(audio),
        "script": str(script_path),
        "overall_match_ratio": round(ratio, 4),
        "threshold": args.threshold,
        "passed": ratio >= args.threshold,
        "whisper_profile": args.profile,
        "transcript_full": full_transcript,
        "script_full": full_script,
        "segments": [{"start": s, "end": e, "text": t} for s, e, t in segments],
        "line_mismatch_count": len(line_issues),
        "line_issues": line_issues,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    print(f"\n=== QA結果 ===")
    print(f"全文一致率: {ratio:.2%}  (しきい値 {args.threshold:.0%})  → {'PASS' if result['passed'] else 'FAIL'}")
    print(f"行単位ミスマッチ: {len(line_issues)}件")
    for it in line_issues[:10]:
        print(f"  L{it['line_index']:02d} {it['speaker']:7s}: {it['expected'][:40]}...")
    print(f"\n詳細: {out_path}")

    if not result["passed"] or line_issues:
        sys.exit(1)


if __name__ == "__main__":
    main()

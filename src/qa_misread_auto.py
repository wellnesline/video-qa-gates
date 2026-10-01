"""VOICEVOX 誤読の自動検出+修正(per-topic)。

検出戦略:
1. 各 line text を Sudachi(独立形態素解析・OpenJTalkとは別辞書)で正規読み取得
2. 同 text を VOICEVOX /audio_query に投げて実読み取得
3. 両方を **長音正規化(おう/おお/おー → 同一)** + **促音吸収** したローマ字化で比較
4. 不一致 line で、Sudachi token 単位で正規読み再取得 → どの kanji compound が誤読か特定
5. **単漢字 + 文脈依存例外**(年月日他何代上下等)はスキップ(false positive 抑制)

修正:
- --apply で当該 surface を Sudachi 読みのひらがな化で置換、line_*.wav 削除
- 後段の synth + gen_bubbles + compose --audio-only で反映

per-topic: VOICEVOX user_dict (engine global) は触らない。文脈依存読みは
script.json text の局所書き換えで完結する(scoped to 1 video)。

ニュース動画モード(--news-date / --news-topic-id):
- ニュース動画は channels/<ch>/videos/news/<date>/<topic_id>/script.json に配置
- --news-date YYYY-MM-DD と --news-topic-id <id> を両方指定することで news 経路を有効化
- news 経路では VOICEVOX 問い合わせ前に news_pipeline.synth.preprocess_text を適用
  (数値単位・略語・記号の正規化でノイズ除去、VOICEVOX 誤認識リスク低減)
- preprocess_text の import に失敗した場合は WARNING を出して fallback(通常モードと同等)
- --news-date / --news-topic-id どちらか片方のみ指定はバリデーションエラー(exit 2)
"""
from __future__ import annotations
import argparse
import difflib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import jaconv
import pykakasi

_PYK = pykakasi.kakasi()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# ============================================================
# news_pipeline.synth.preprocess_text (optional import)
# ============================================================
_news_preprocess: "callable[[str], str] | None" = None
try:
    from news_pipeline.synth import preprocess_text as _news_preprocess_fn  # type: ignore
    _news_preprocess = _news_preprocess_fn
except Exception:
    pass  # fallback: warn at runtime when news mode is active

# ============================================================
# 文脈依存単漢字 — VOICEVOX が文脈で正しく読むため検出スキップ
# ============================================================
SKIP_SINGLE_KANJI = set(
    "年月日時分秒週" +
    "代回個本枚冊台匹頭羽人組" +
    "上下左右前後内外中先後" +
    "一二三四五六七八九十百千万億" +
    "他何何位"
)

KANJI_RE = re.compile(r"[一-鿿々〆ヶ]")


def has_kanji(s: str) -> bool:
    return bool(KANJI_RE.search(s))


# ============================================================
# kana 正規化(長音吸収)
# ============================================================
def kana_to_norm_roma(s: str) -> str:
    """カタカナ/ひらがな/長音 → 全部小文字ローマ字+長音記号統一+チョーオン折りたたみ。"""
    h = jaconv.kata2hira(s)
    r = jaconv.kana2alphabet(h)  # ローマ字
    r = r.lower()
    # 長音記号「ー」は前母音の repeat → 折りたたみ
    r = re.sub(r"([aiueo])-", r"\1", r)
    # おう/おお → o, えい/ええ → e (連母音同化)
    r = re.sub(r"o[uo]", "o", r)
    r = re.sub(r"e[ei]", "e", r)
    # 促音(ッ): 「ttsu」「kkt」等を整理
    r = re.sub(r"([ksctnhmyrwgzdbp])\1", r"\1", r)
    # 連続母音重複(うう、おお等)
    r = re.sub(r"([aiueo])\1", r"\1", r)
    return r


# ============================================================
# Sudachi
# ============================================================
def _sudachi_tokenizer():
    from sudachipy import dictionary, tokenizer
    tk = dictionary.Dictionary().create()
    return tk, tokenizer.Tokenizer.SplitMode.C  # C = 最長単位


def sudachi_tokens(text: str, tk_mode):
    tk, mode = tk_mode
    out = []
    for m in tk.tokenize(text, mode):
        s = m.surface()
        r = m.reading_form()  # カタカナ
        if not r:
            r = s
        out.append((s, r))
    return out


# ============================================================
# VOICEVOX
# ============================================================
def default_speaker_id() -> int:
    """話者 ID の既定値は持たない。環境変数 VOICEVOX_SPEAKER_ID か channel.yaml の voicevox_id で渡す。"""
    raw = os.environ.get("VOICEVOX_SPEAKER_ID", "").strip()
    if not raw.isdigit():
        raise RuntimeError(
            "VOICEVOX の話者 ID が未設定です。環境変数 VOICEVOX_SPEAKER_ID を設定するか、"
            "channel.yaml の characters.<slot>.voicevox_id で指定してください(既定値はありません)"
        )
    return int(raw)


def voicevox_reading(text: str, speaker_id: int | None = None, base_url: str = "http://127.0.0.1:50021") -> str:
    """VOICEVOX /audio_query から実 mora 連結を返す(カタカナ)。speaker_id が None なら default_speaker_id()。"""
    if speaker_id is None:
        speaker_id = default_speaker_id()
    url = base_url + "/audio_query?" + urllib.parse.urlencode({"text": text, "speaker": speaker_id})
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        q = json.loads(resp.read())
    moras = []
    for ph in q.get("accent_phrases", []):
        for m in ph.get("moras", []):
            moras.append(m.get("text", ""))
    return "".join(moras)


# ============================================================
# 検出
# ============================================================
def find_misreads_in_line(
    text: str,
    sid: int | None,
    tk_mode,
    preprocess_fn: "callable[[str], str] | None" = None,
) -> list[dict]:
    """line text 内の誤読 candidate を返す。

    アルゴリズム:
    1. 全文 Sudachi 読み(連結)と全文 VOICEVOX 読みを正規化比較
    2. 一致なら misread なし
    3. 不一致なら、Sudachi token 単位で「kanji 含み + SKIP外 + 自身の読みが VV と異なる」もの抽出

    preprocess_fn: VOICEVOX 問い合わせ前に text へ適用する変換関数(news モード用)。
                   None の場合は変換なし。
    """
    # news_pipeline.synth.preprocess_text を適用(記号・略語・単位の正規化)
    vv_text = preprocess_fn(text) if preprocess_fn is not None else text
    sud_tokens = sudachi_tokens(text, tk_mode)
    sud_full = "".join(r for _, r in sud_tokens)
    vv_full = voicevox_reading(vv_text, sid)
    if kana_to_norm_roma(sud_full) == kana_to_norm_roma(vv_full):
        return []

    misreads = []
    # token 単位で再精査
    # マージルール:
    #  (a) 隣接 kanji token は連結
    #  (b) kanji + 短い助詞(の/が/に/を/と/は/へ・1文字)+ kanji → 連結
    #     例「他の日」「彼の本」を1単位として misread 検出する
    PARTICLES_MERGE = set("のがにをとはへ")
    merged_compounds = []  # [(surface, sudachi_reading)]
    buf_s, buf_r = "", ""
    i = 0
    while i < len(sud_tokens):
        s, r = sud_tokens[i]
        if has_kanji(s):
            buf_s += s
            buf_r += r
            # lookahead: 次が単一助詞 + その次が kanji なら連結
            if (i + 2 < len(sud_tokens)
                and len(sud_tokens[i + 1][0]) == 1
                and sud_tokens[i + 1][0] in PARTICLES_MERGE
                and has_kanji(sud_tokens[i + 2][0])):
                buf_s += sud_tokens[i + 1][0]
                buf_r += sud_tokens[i + 1][1] or sud_tokens[i + 1][0]
                # 次の kanji を続けて取り込むため、次の loop で処理
                i += 1  # skip particle
        else:
            if buf_s:
                merged_compounds.append((buf_s, buf_r))
                buf_s, buf_r = "", ""
        i += 1
    if buf_s:
        merged_compounds.append((buf_s, buf_r))

    for comp_s, comp_r in merged_compounds:
        # 単漢字 + 文脈依存スキップ
        if len(comp_s) == 1 and comp_s in SKIP_SINGLE_KANJI:
            continue
        # token 単独で VOICEVOX 問い合わせ
        try:
            vv_alone = voicevox_reading(comp_s, sid)
        except Exception:
            continue
        # pykakasi 第3エンジン(majority vote用、OpenJTalk非依存)
        pyk_kata = "".join(r["kana"] for r in _PYK.convert(comp_s))
        sud_norm = kana_to_norm_roma(comp_r)
        vv_norm = kana_to_norm_roma(vv_alone)
        pyk_norm = kana_to_norm_roma(pyk_kata)
        # 両方空 or VV一致 → skip
        if not sud_norm or not vv_norm or sud_norm == vv_norm:
            continue
        ratio = difflib.SequenceMatcher(None, sud_norm, vv_norm).ratio()
        if ratio >= 0.85:
            continue
        # 活用形 artefact skip: VV読みが Sudachi読みで始まる/終わる(差≤2chars)
        # → 動詞連用形に「て/だ/た」付加読みされた artefact
        if vv_norm.startswith(sud_norm) and 0 < len(vv_norm) - len(sud_norm) <= 2:
            continue
        if vv_norm.endswith(sud_norm) and 0 < len(vv_norm) - len(sud_norm) <= 2:
            continue
        # === Majority vote ===
        # VV が Sudachi/pykakasi のどちらかと一致 → VV正解扱いで skip(false positive 防止)
        if pyk_norm and (vv_norm == pyk_norm):
            continue
        # Sudachi と pykakasi が一致 → 高信頼の misread
        # それ以外でも、VV が両方と「乖離大」(両 ratio < 0.7) なら misread 候補
        ratio_sud = ratio  # 既に計算済 (vv vs sud)
        ratio_pyk = difflib.SequenceMatcher(None, vv_norm, pyk_norm).ratio() if pyk_norm else 0.0
        if pyk_norm and sud_norm == pyk_norm:
            confidence = "high (Sudachi+pykakasi agreed)"
        elif ratio_sud < 0.75 and ratio_pyk < 0.85:
            confidence = f"medium (VV diverges from both: r_sud={ratio_sud:.2f}/r_pyk={ratio_pyk:.2f})"
        else:
            # 1エンジンとは近い → ambiguous skip
            continue
        fix_text = jaconv.kata2hira(comp_r)
        misreads.append({
            "surface": comp_s,
            "sudachi_kana": comp_r,
            "pykakasi_kana": pyk_kata,
            "voicevox_kana": vv_alone,
            "sudachi_norm": sud_norm,
            "pykakasi_norm": pyk_norm,
            "voicevox_norm": vv_norm,
            "ratio_vs_sud": round(ratio_sud, 2),
            "ratio_vs_pyk": round(ratio_pyk, 2),
            "fix_replace_text": fix_text,
            "confidence": confidence,
        })
    return misreads


def detect_per_line(
    script_path: Path,
    vv_speaker_map: dict,
    tk_mode,
    preprocess_fn: "callable[[str], str] | None" = None,
) -> list[dict]:
    data = json.loads(script_path.read_text(encoding="utf-8"))
    issues = []
    for i, ln in enumerate(data["lines"]):
        text = ln.get("text", "")
        if not text or ln.get("is_silence"):
            continue
        speaker = ln.get("speaker", "speaker_b")
        sid = vv_speaker_map.get(speaker)  # 未登録の話者は voicevox_reading() が VOICEVOX_SPEAKER_ID を使う(無ければエラー)
        ms = find_misreads_in_line(text, sid, tk_mode, preprocess_fn=preprocess_fn)
        if ms:
            issues.append({
                "line_index": i,
                "speaker": speaker,
                "text": text,
                "misreads": ms,
            })
    return issues


# ============================================================
# 修正
# ============================================================
def apply_fixes(script_path: Path, issues: list[dict]) -> tuple[int, list[int]]:
    data = json.loads(script_path.read_text(encoding="utf-8"))
    n_fixed = 0
    fixed_lines = set()
    for issue in issues:
        idx = issue["line_index"]
        ln = data["lines"][idx]
        text = ln["text"]
        for m in issue["misreads"]:
            surface = m["surface"]
            fix = m["fix_replace_text"]
            if surface in text:
                text = text.replace(surface, fix)
                n_fixed += 1
                fixed_lines.add(idx)
        ln["text"] = text
    if n_fixed:
        script_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return n_fixed, sorted(fixed_lines)


# ============================================================
# パス解決
# ============================================================
def _resolve_paths(args) -> tuple[Path, Path]:
    """topic_dir と script_path を解決して返す。

    通常モード: channels/<ch>/videos/<topic>/script.json
    Shorts モード(--short-id): channels/<ch>/videos/<topic>/shorts/<id>/script.json
    ニュースモード(--news-date + --news-topic-id):
        channels/<ch>/videos/news/<news_date>/<news_topic_id>/script.json
        topic_dir は channels/<ch>/videos/news/<news_date>/<news_topic_id>/
    存在しない場合は (topic_dir, None) — 呼び出し側が None チェックすること。
    """
    news_date = getattr(args, "news_date", None)
    news_topic_id = getattr(args, "news_topic_id", None)
    if news_date and news_topic_id:
        topic_dir = ROOT / "channels" / args.channel / "videos" / "news" / news_date / news_topic_id
        script_path = topic_dir / "script.json"
    elif getattr(args, "short_id", None):
        topic_dir = ROOT / "channels" / args.channel / "videos" / args.topic
        # 本ch/全ch標準: shorts/short-<id>.json (script file) + shorts/short-<id>/voice/L*.wav
        script_path = topic_dir / "shorts" / f"short-{args.short_id}.json"
    else:
        topic_dir = ROOT / "channels" / args.channel / "videos" / args.topic
        script_path = topic_dir / "script.json"
    if not script_path.exists():
        return topic_dir, None
    return topic_dir, script_path


# ============================================================
# CLI
# ============================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--channel", required=True)
    ap.add_argument("--apply", action="store_true",
                    help="検出した誤読を script.json に書き戻す")
    ap.add_argument("--clear-wav", action="store_true",
                    help="--apply時、修正対象行の line_*.wav を削除(再 synth用)")
    ap.add_argument("--short-id", dest="short_id", default=None,
                    help="Shorts スクリプトを対象にする場合の short ID (例: A)")
    ap.add_argument("--force", action="store_true",
                    help="channel.yaml の qa.misread_check=false でもスキップせず実行する")
    ap.add_argument("--news-date", dest="news_date", default=None,
                    help="ニュース動画モード: 対象日付 YYYY-MM-DD (--news-topic-id と同時指定必須)")
    ap.add_argument("--news-topic-id", dest="news_topic_id", default=None,
                    help="ニュース動画モード: トピック ID (例: cut_a / cut_b)")
    args = ap.parse_args()

    # --- news フラグ バリデーション ---
    news_date = getattr(args, "news_date", None)
    news_topic_id = getattr(args, "news_topic_id", None)
    if bool(news_date) != bool(news_topic_id):
        print(
            "[error] --news-date と --news-topic-id は両方同時に指定してください(片方のみは無効)",
            file=sys.stderr,
        )
        return 2

    import yaml
    cfg = yaml.safe_load((ROOT / "channels" / args.channel / "channel.yaml").read_text(encoding="utf-8"))

    # --- opt-in ゲート ---
    # 対象ch のみ実行(channel.yaml に qa.misread_check: true が必要・未設定/falseは skip)
    # --force で bypass 可能(実験/CI 用)
    if not args.force:
        qa_cfg = cfg.get("qa") or {}
        if not qa_cfg.get("misread_check", False):
            print(f"[skip] qa.misread_check unset/false in {args.channel}/channel.yaml (opt-in 必須・--force で bypass)")
            return 0

    topic_dir, script_path = _resolve_paths(args)
    if script_path is None:
        print(f"script.json not found: {topic_dir}", file=sys.stderr)
        return 2

    chars = cfg.get("characters", {})
    vv_speaker_map = {}
    for slot in ("speaker_explainer", "speaker_reactor"):
        s = chars.get(slot) or {}
        prefix = s.get("portrait_prefix") or s.get("name")
        sid = s.get("voicevox_id")
        if prefix and sid is not None:
            vv_speaker_map[prefix] = int(sid)
    print(f"[init] speaker_map: {vv_speaker_map}")

    # --- news 経路: preprocess_text の準備 ---
    preprocess_fn = None
    if news_date and news_topic_id:
        if _news_preprocess is not None:
            preprocess_fn = _news_preprocess
            print("[news] preprocess_text を VOICEVOX 問合せ前に適用")
        else:
            print(
                "[warn] news_pipeline.synth.preprocess_text の import に失敗。"
                "fallback: preprocess なしで続行",
                file=sys.stderr,
            )

    tk_mode = _sudachi_tokenizer()

    print(f"[detect] scanning {script_path.name} ...")
    issues = detect_per_line(script_path, vv_speaker_map, tk_mode, preprocess_fn=preprocess_fn)

    n_total = sum(len(i["misreads"]) for i in issues)
    print(f"\n=== 検出: {len(issues)} line / 計 {n_total} 誤読候補 ===")
    for issue in issues:
        print(f"\nL{issue['line_index']:02d} [{issue['speaker']}] {issue['text']}")
        for m in issue["misreads"]:
            print(f"  ⚠️  「{m['surface']}」 期待:{m['sudachi_kana']} (pyk:{m.get('pykakasi_kana','')}) 実読:{m['voicevox_kana']} r_sud={m.get('ratio_vs_sud','-')}/r_pyk={m.get('ratio_vs_pyk','-')} [{m.get('confidence','')}] → fix「{m['fix_replace_text']}」")

    if args.apply and issues:
        n, fixed_lines = apply_fixes(script_path, issues)
        print(f"\n[apply] {n} 件 patch → script.json 更新済み (lines {fixed_lines})")
        if args.clear_wav and fixed_lines:
            # 経路別に wav パス分岐:
            #  - 本編: work/line_{li:02d}.wav  (0-indexed・固定パターン)
            #  - Shorts: shorts/short-<id>/voice/{ln['id']}.wav  (id ベース・例 L01.wav)
            #  - News: voice/{ln['id']}.wav  (id ベース・1-indexed L01.wav)
            data = json.loads(script_path.read_text(encoding="utf-8"))
            news_date = getattr(args, "news_date", None)
            news_topic_id = getattr(args, "news_topic_id", None)
            short_id = getattr(args, "short_id", None)
            if news_date and news_topic_id:
                wav_dir = topic_dir / "voice"
                speech_norm_files = ["voice_concat.wav", "voice_concat_norm_i16.wav"]
                wav_filename = lambda li, ln: f"{ln['id']}.wav"
            elif short_id:
                wav_dir = topic_dir / "shorts" / f"short-{short_id}" / "voice"
                speech_norm_files = ["voice_concat.wav", "voice_concat_norm_i16.wav"]
                wav_filename = lambda li, ln: f"{ln['id']}.wav"
            else:
                wav_dir = topic_dir / "work"
                speech_norm_files = ["speech_norm.wav", "speech_norm_i16.wav"]
                wav_filename = lambda li, ln: f"line_{li:02d}.wav"
            for li in fixed_lines:
                ln = data["lines"][li]
                wav = wav_dir / wav_filename(li, ln)
                if wav.exists():
                    wav.unlink()
                    print(f"  cleared {wav.name}")
            for nf in speech_norm_files:
                p = wav_dir.parent / nf if (short_id or (news_date and news_topic_id)) else wav_dir / nf
                if p.exists():
                    p.unlink()
                    print(f"  cleared {nf}")

    # out_path 経路別:
    #  - 本編: output/qa_misread_auto.json
    #  - Shorts: shorts/short-<id>/qa_misread_auto.json
    #  - News: <topic_dir>/qa_misread_auto.json
    if getattr(args, "news_date", None) and getattr(args, "news_topic_id", None):
        out_path = topic_dir / "qa_misread_auto.json"
    elif getattr(args, "short_id", None):
        out_path = topic_dir / "shorts" / f"short-{args.short_id}" / "qa_misread_auto.json"
    else:
        out_path = topic_dir / "output" / "qa_misread_auto.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"issues": issues}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[done] report → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

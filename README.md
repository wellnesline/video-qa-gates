# video-qa-gates

A collection of automated quality gates for generated videos: freeze frames, loudness, BGM presence, scene diversity, source minimums, opening hook, TTS misreadings and Shorts specs.

自動生成した動画を公開前に機械検査するゲート（QA スクリプト）集です。公開用に整理したサンプル実装です。

## 概要

動画の自動生成では、「映像が止まる」「BGM が抜ける」「音量が規格外」「同じ素材の使い回し」「読み上げの誤読」といった事故が、人の目に触れる前に起きます。このリポジトリは、そうした事故を判定の実体（動画ファイル・タイムライン・台本・音声）から直接検査する単機能スクリプトを集めたものです。各スクリプトは単体で実行でき、結果は終了コードとレポートで返ります。

## 主な機能・設計のポイント

- **二段ガード**: ビルド時の簡易検査に加え、最終の mp4 を独立に再検査します（例: `qa_bgm_presence.py` は音声チャンネル数と簡易ラウドネスで BGM 層の実在を確認）
- **映像フリーズ検出**: `qa_freezedetect.py`（ffmpeg `freezedetect`。長いフリーズは FAIL、短いものは WARN。プロファイルごとのしきい値を設定ファイルから取得可）と `qa_freeze_check.py`。`qa_scene_time_bounds.py` は、素材の尺を超える指定が末尾フレームのループ（停止）を生む事故をビルド前に防ぎます
- **ラウドネス規格**: `qa_loudness_target.py` が統合ラウドネス（-16 LUFS ± 2.5 dB）とトゥルーピーク上限を検査
- **素材構成のハードガード**: `qa_source_minimums.py`（ライセンスが明示された動画素材が最低 1 つ必要、AI 画像だけで 100% にしない、AI 画像枚数の上限、比率が高いと警告）、`qa_scene_diversity.py` / `qa_shorts_visual_diversity.py`（同一素材の使用回数や連続の制限）
- **冒頭フック**: `qa_opening_hook.py` が、企画段階で記録したピーク位置が台本の冒頭 3 行に転記されているか、素材リストに無い素材を参照していないかを検査
- **誤読の検出**: `qa_misread_auto.py` が形態素解析（Sudachi）の読みと VOICEVOX の実読みを、長音・促音を正規化して比較し、不一致の語を特定。`qa_transcribe.py` は音声を文字起こしして台本と差分比較します
- **Shorts 仕様**: `qa_shorts_rebuild.py` が縦型解像度・尺などを機械検査。`qa_primer_shorts.py` は Shorts の設定 JSON と（あれば）動画を検査（尺・行数・字数・話者リスト・禁止語は環境変数 `QA_SHORTS_CONFIG` の JSON で渡します。既定値は汎用のサンプルです）
- **回帰テスト**: `tests/` に、台本 QA（小数点の誤読、装飾記号の BLOCK、数字のかな化、ffprobe 失敗時の WARN への降格、解像度の BLOCK、共有 ID の除外 など）の単体テスト。WARN にとどめるか BLOCK にするかを、偽陽性の出方に応じて切り分ける設計です

## ディレクトリ構成（主要ファイル）

```
video-qa-gates/
├ src/
│  ├ qa_freezedetect.py  qa_freeze_check.py  qa_scene_time_bounds.py
│  ├ qa_bgm_presence.py  qa_loudness_target.py
│  ├ qa_source_minimums.py  qa_scene_diversity.py  qa_shorts_visual_diversity.py
│  ├ qa_opening_hook.py  qa_misread_auto.py  qa_transcribe.py
│  ├ qa_shorts_rebuild.py  qa_primer_shorts.py
│  └ channel_resolver.py  prebuild_layers.py     ← 合成スタブ
└ tests/      test_qa_*.py
```

## 動かし方

- 必要なもの: Python 3、ffmpeg / ffprobe（`FFMPEG_BIN` / `FFPROBE_BIN` で場所を指定可能）、下記「同梱していない依存」のうち使うスクリプトに必要なもの
- 誤読検出には VOICEVOX エンジン（ローカルのポート 50021）が必要です
- 環境変数: `CHANNEL` / `DEFAULT_CHANNEL`（設定の切り替え名）、`ALLOW_AI_HEAVY`（AI 画像比率の例外許可）、`VOICEVOX_SPEAKER_ID`（誤読検出で使う話者 ID。**既定値なし**。`channel.yaml` の `characters.<slot>.voicevox_id` で行ごとに指定する場合は不要。どちらも無いと `qa_misread_auto.py` は明示的なエラーで止まります）
- 実行例: `python src/qa_freezedetect.py --help`（各スクリプトは `argparse`。対象トピックや動画パスを引数で渡します）
- テスト: 兄弟リポジトリを `src/` 配下に配置したうえで `pytest tests/`

## 制限事項（閉包）

`ast` と import 文の調査で、リポジトリ内・標準ライブラリに無く、`requirements.txt` も無いため、次を同梱していない依存として挙げます。

- **同梱していない Python パッケージ（6 件）**: `yaml`（PyYAML）, `faster_whisper`, `pykakasi`, `jaconv`, `sudachipy`, `pytest`
- **他の公開リポジトリへの依存（2 件）**: `slide-to-video-pipeline`（`src.post_video.qa_post_video` / `slides` / `paths`。tests が参照）, `news-video-pipeline`（`news_pipeline.synth` / `news_pipeline.qa`）
- **合成スタブ**: `src/channel_resolver.py`（最小のチャンネル解決）, `src/prebuild_layers.py`（シーン多様性ルールのうち 2 つだけを実装した簡易版）
- **非同梱の実行時データ**: チャンネル設定（`channel.yaml`）、QA プロファイル設定（`config/qa_profile.yaml`）、検査対象の動画・台本・素材リスト
- 外部サービス: VOICEVOX（ローカル）。音声合成のキャラクター・素材は、各利用規約と権利者の許諾の範囲で使ってください

## ライセンス

All rights reserved. 閲覧目的で公開しています。詳細は [LICENSE](LICENSE) を参照。

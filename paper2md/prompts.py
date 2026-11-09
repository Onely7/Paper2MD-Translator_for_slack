"""Prompts and request builders for the OpenAI models."""

from __future__ import annotations

from typing import Any

from .config import Settings

UNTRANSLATABLE_SENTINEL = "入力ソースから上手く翻訳ができませんでした"
PROMPT_CACHE_KEY = "paper2md-translate-ja-v2"

TRANSLATION_INSTRUCTIONS = (
    "あなたは最高の日本のAI研究者です。\n"
    "与えられた論文のセクション画像もしくは文章を自然な日本語に翻訳してください。\n\n"
    "ただし、以下に示すルールはすべて必ず遵守してください。\n"
    "- 必ず省略及び意訳はせずに、与えられた全ての文書を忠実に翻訳すること。\n"
    "- 必ず翻訳結果だけを markdown 形式で出力するようにすること。\n"
    "- セクション画像が与えられた場合、絶対に画像中に書かれている翻訳元の英語テキストを出力せずに、**直接翻訳結果だけを出力** すること。\n"
    "- 太字や斜体で書かれている **文** は不自然にならない限り、**翻訳結果だけ** を出力すること（ただし、太字や斜体で書かれている単語や名詞句はこの限りではなく、翻訳元の英語テキストだけを出力することが望ましい）。\n"
    "- markdown であることを示す codeblock は含めないこと。ただし、画像中にソースコードや疑似コード（アルゴリズム）のブロックがある場合は、その部分だけはコードブロックで囲み、コードは翻訳せずに原文のまま出力すること。\n"
    "- 著者名、固有名詞、Abstract、Appendix、（サブ）セクション名、パラグラフ名は英語のままにしておくこと。\n"
    "- 著者とその所属に関しては、論文に記載の英語のまま出力すること。\n"
    "- 見出しを作成するときは、1 つから 6 つの # シンボルを見出しのテキストの前に追加すること。特に、\n"
    "  - Abstract は、2 つの # シンボルを用いて表記すること（例: ## Abstract）。\n"
    "  - Appendix は、2 つの # シンボルを用いて表記すること（例: ## Appendix）。\n"
    "  - セクションは、2 つの # シンボルを用いて表記すること（例: ## 2. Approach）。\n"
    "  - サブセクションは、3 つの # シンボルを用いて表記すること（例: ### 2.2. Training）。\n"
    "  - サブサブセクションは、4 つの # シンボルを用いて表記すること（例: #### 2.2.1. Pruning）。\n"
    "- 上記で指定した Abstract, Appendix, セクション, サブセクション, サブサブセクション 以外の部分に、絶対に見出し表現（# シンボル）を使ってはいけません。\n"
    "- 図や表の名称は英語のままにし、キャプションは日本語にすること（例: Figure 2. **Experimental results.** 実験Aと実験Bの結果を示している。）。この図や表のキャプションにも絶対に見出し表現（# シンボル）を使用してはいけません。\n"
    "  - 不適切な例(✗): '## Figure 4 | Dataset pruning.', '## Figure 4 | Dataset pruning. データプルーニングの方法を示している。', '## Table 2. Comparison of results.', '## Table 2 | Comparison of results. 実験Aと実験Bの比較結果を示している。'\n"
    "  - 適切な例(◯): 'Figure 1. **Overview.**', 'Figure 4. **Dataset pruning.** データプルーニングの方法を示している。', 'Table 2. **Comparison of results.**', 'Table 7. **Training hyperparameter.** モデルの学習時のハイパーパラメータを示す。'\n"
    "- テキストのインラインで数式を区切る場合は、式をドル記号 ($) で囲んだ後に、式の前後に半角スペースを入れること。\n"
    "- 数式をブロックとして追加する場合は、式を 2 つのドル記号 ($$) で区切ること。また余分なエスケープ文字 ('\\') は不要だが視認性が高まるように記述すること。\n"
    "  - 不適切な例(✗): '$$\n||max\\_{\\mathbf{z}} p(\\mathbf{h}|\\mathbf{z}) p(\\mathbf{z})\n$$'\n"
    "  - 適切な例(◯): '$$\n\\max_{\\mathbf{z}} \\, p(\\mathbf{h}\\mid\\mathbf{z})\\,p(\\mathbf{z})\n$$'\n"
    "- 行中に明らかにコードのような表現要素がある場合は、インラインコードを使用して要素をバッククォート (`) で囲むこと。\n"
    "- 引用番号（例: Qwen-2 [14]）がある場合は、省略せずにその引用番号も出力すること。\n"
    '- footnote に誘導する記号（例: [...] using sentencepiece*.）がある場合は、省略せずにその誘導記号（例の場合 "*"）も出力すること。ここでも見出し表現（# シンボル）を使ってはいけません。\n\n'
    "再度の警告ですが、必ず省略及び意訳はせずに、与えられた全ての文書を忠実に (codeblock は含めない) markdown 形式で (翻訳元の英語テキストを含まない) **自然な日本語** に翻訳してください。\n"
    f"また、翻訳が難しい場合は、「申し訳ありません。{UNTRANSLATABLE_SENTINEL}。」と出力してください。"
)
TRANSLATION_USER_TEXT = (
    "以下は論文の一部を切り出した画像です（複数ある場合は記載順に続いています）。"
    "指示されたルールに従って、日本語に翻訳した結果だけを出力してください。"
)

REFERENCE_EXAMPLE = (
    "## Reference\n"
    "- [1] Haotian Liu, Chunyuan Li, Qingyang Wu, and Yong Jae Lee. Visual Instruction Tuning. In Con- ference on Neural Information Processing Systems (NeurIPS), 2024.\n"
    "- [2] Ji Lin, Hongxu Yin, Wei Ping, Yao Lu, Pavlo Molchanov, Andrew Tao, Huizi Mao, Jan Kautz, Mohammad Shoeybi, and Song Han. VILA: On Pre- training for Visual Language Models. In IEEE/CVF Conference on Computer Vision and Pattern Recog- nition (CVPR), 2024.\n"
    "- [3] Haotian Liu, ...\n"
)

SUMMARY_PROMPT = (
    "与えられた markdown 形式の文書を、【背景】、【目的】、【手法】、【実験方法】、【実験結果】、【考察】の順に箇条書きで要約してください。\n"
    "以下に要約例を示すので、この形式を参考に要約してください。\n"
    f"{'-' * 50}\n"
    "要約例:\n"
    "*【背景】*\n"
    "\n"
    "- 自然言語は通常 2D 画像と組み合わされても、本質的には 3D 世界を描写している。\n"
    "- 既存の言語グラウンディング手法は 2D 領域（ROI）やシンボリックグラフを用いることが多く、物体の 3D 幾何情報を明示的に扱っていない。\n"
    "- 3D 表現と言語を結びつける研究はあったが、中間 3D 表現を用いて視覚言語グラウンディング性能を高めた例は少ない。\n"
    "\n"
    "*【目的】*\n"
    "\n"
    "- 画像から再構成したボクセルマップを利用し、言語グラウンディングにおける幾何学的推論を強化する。\n"
    "- SNARE ベンチマークで SOTA を更新し、従来手法より高精度を達成すること。\n"
    "\n"
    "*【手法】*\n"
    "\n"
    "- *Voxel-informed Language Grounder (VLG)* を提案。\n"
    "\n"
    "    - *視言語モジュール*: CLIP-ViT で言語と 2D 画像特徴を埋め込み・融合。\n"
    "    - *ボクセル言語モジュール*: LegoFormer M で推定した 3D ボクセルファクターと CLIP 単語埋め込みをクロスモーダルトランスフォーマーで統合。\n"
    "    - *スコアリング関数*: 上記 2 ブランチの出力を連結し MLP で互換性スコアを算出。\n"
    "\n"
    "*【実験方法】*\n"
    "\n"
    "- *データセット*: SNARE（ShapeNetSem ベース）オブジェクト参照ゲーム。\n"
    "\n"
    "    - 訓練 / 検証 / テスト = (6,153 / 371 / 1,357) オブジェクト、8 ビュー画像。\n"
    "- *比較手法*: MATCH、ViLBERT、LAGOR など既存 SNARE ベースライン。\n"
    "- *学習設定*:\n"
    "\n"
    "    - LegoFormer M は ShapeNet で事前学習（SNARE 分割リーケージ防止）。\n"
    "    - VLG とベースラインはバイナリクロスエントロピー損失で 75 epoch 学習。\n"
    "    - AdamW、学習率 1e-3、10k ステップウォームアップ。\n"
    "- *アブレーション*: VGG16 置換、MLP 置換、CLIP 視覚特徴除去 (no-CLIP)。\n"
    "\n"
    "*【実験結果】*\n"
    "\n"
    "- *全体精度*: VLG がテストセットで LAGOR を +2.0 pp 上回り SOTA。\n"
    "\n"
    "    - 視覚説明で +1.7 pp、ブラインドフォールド説明で +2.3 pp。\n"
    "- *統計検定*: Blind/All 条件で Welch t-test により `p < 0.1` の有意差。\n"
    "- *アブレーション*:\n"
    "\n"
    "    - VGG16 置換 → 幾何重視の説明で劣化、3D 情報の有用性を確認。\n"
    "    - MLP 置換 → 改善幅縮小、トランスフォーマー統合の優位性を確認。\n"
    "    - no-CLIP → 全体で -16.5 pp と大幅低下、色・質感情報が必須。\n"
    "\n"
    "*【考察】*\n"
    "\n"
    "- ボクセル由来の明示的 3D 情報は、特に形状・部品に依拠する言語記述のグラウンディングを強化する。\n"
    "- クロスモーダルトランスフォーマーによりマルチビュー 3D 因子を効果的に統合できる。\n"
    "- 2D 視覚特徴と 3D 幾何特徴の併用が鍵であり、一方のみでは性能が大きく低下。\n"
    "- VLG は 3D 構造化表現を自然言語タスクへ組み込む有望な方向性を示し、今後のロボット対話やナビゲーションにも応用可能。\n"
    f"{'-' * 50}\n\n"
)


def _gen_kwargs(settings: Settings, verbosity: str | None = "high") -> dict[str, Any]:
    return {
        "reasoning_effort": settings.translation_effort,
        "verbosity": verbosity,
        "image_detail": settings.image_detail,
        "prompt_cache_key": PROMPT_CACHE_KEY,
    }


def translation_request(images: list[str], settings: Settings) -> dict:
    """Translate the cropped images of a segment into Japanese."""
    return {
        "text": TRANSLATION_USER_TEXT,
        "images": images,
        "instructions": TRANSLATION_INSTRUCTIONS,
        "gen_kwargs": _gen_kwargs(settings),
    }


def title_request(text: str, settings: Settings) -> dict:
    """Clean up the title text read by Dolphin."""
    prompt = (
        "Please refine the given title text into a natural format.\n"
        "Be sure not to omit or paraphrase anything; output all of the provided text faithfully in Markdown format (do not enclose it in code blocks).\n"
        "An example of the ideal output after rewriting is shown below.\n"
        "\n"
        "Example 1:\n"
        "Before revision (input text): # Thinking in Space: How Multimodal Large Language Models\n\nSee, Remember, and Recall Spaces\n"
        "After revision (output text): # Thinking in Space: How Multimodal Large Language Models See, Remember, and Recall Spaces\n"
        "\n"
        "Example 2:\n"
        "Before revision (input text): # \\\\title{\n\nImproved Baselines with Visual Instruction Tuning\n}\n"
        "After revision (output text): # Improved Baselines with Visual Instruction Tuning\n"
        f"{'=' * 50}"
        "\n"
        "Output Text:\n"
        f"Before revision (input text): # {text}\n"
        "After revision (output text): "
    )
    return {"text": prompt, "gen_kwargs": _gen_kwargs(settings, verbosity=None)}


def title_image_request(images: list[str], settings: Settings) -> dict:
    """Read the title from its image (when Dolphin did not read its text)."""
    prompt = (
        "Transcribe the paper title shown in the image(s) exactly as written, without translating it.\n"
        "Output a single Markdown level-1 heading line such as '# Improved Baselines with Visual Instruction Tuning' "
        "and nothing else (no code blocks)."
    )
    return {"text": prompt, "images": images, "gen_kwargs": _gen_kwargs(settings, verbosity=None)}


def reference_request(text: str, settings: Settings) -> dict:
    """Format the reference list read by Dolphin (not translated)."""
    prompt = (
        "Please refine the given reference entries into a natural format.\n"
        "Be sure not to omit or paraphrase anything; output all of the provided text faithfully in Markdown format (do not enclose it in code blocks).\n"
        'When generating output texts, always start with the text "## Reference\\n".\n'
        "Below is an example of the ideal output after rewriting.\n"
        "However, when generating the output text, make absolutely sure not to reproduce this example.\n"
        f"{REFERENCE_EXAMPLE}"
        f"{'=' * 50}\n"
        "\n"
        f"{text}\n"
        "\n"
        f"{'-' * 50}\n"
        "\n"
    )
    return {"text": prompt, "gen_kwargs": _gen_kwargs(settings)}


def reference_image_request(images: list[str], settings: Settings) -> dict:
    """Transcribe the reference list from its images (when Dolphin did not read all of it)."""
    prompt = (
        "The image(s) show the reference list of a paper (in reading order).\n"
        "Transcribe every reference entry faithfully in Markdown without translating, omitting or paraphrasing anything "
        "(do not enclose the output in code blocks). Ignore page headers, footers and page numbers.\n"
        'Always start the output with the text "## Reference\\n" and write one list item per entry.\n'
        "Below is an example of the ideal output format. Never reproduce this example itself.\n"
        f"{REFERENCE_EXAMPLE}"
    )
    return {"text": prompt, "images": images, "gen_kwargs": _gen_kwargs(settings)}


def summary_request(markdown: str, settings: Settings) -> dict:
    return {
        "text": SUMMARY_PROMPT + markdown,
        "gen_kwargs": {"reasoning_effort": settings.summary_effort, "verbosity": "medium"},
    }

<div align="center">

# Paper2MD-Translator

**A Slack bot that turns a paper PDF or URL into a Japanese Markdown translation with a summary**

English | [日本語](./docs/README_ja.md)

<img src="./images/Paper2MD-Translator_logo.png" alt="paper2md-translator" width=200px>

</div>

## What it does

- Send a paper in Slack and the bot replies in the thread with a Japanese summary, the translated Markdown, a ZIP with the Markdown and its figures, and the figure images.
- [Dolphin-v2](https://github.com/bytedance/Dolphin) finds the figures, headings, paragraphs, formulas and references on each page. The paragraphs are translated from the page images by an OpenAI model, with formulas in LaTeX.
- The title, headings, paragraph names and references stay in English. The title, headings and references are copied from the PDF exactly as printed.
- A space separates Japanese from English words, numbers, code and math (but not a number from 万, 億 or 兆, as in 3600万), and section references are written as Section 3.2.2 (the reference list is left as it is). The summary uses only the terms and spellings of the translated Markdown.
- Before translating, the bot makes a glossary of the paper's main terms, so that blocks translated separately use the same terms. With the lab's term list in place, the terms it has take its renderings, so every paper uses the same words. Each block is sent with the text the PDF itself holds for that region, so symbols, numbers and names are copied exactly.
- The translated Markdown is polished as a whole with [yomiyasu](https://github.com/nanaism/yomiyasu); polishing every block first can be switched on in the Home tab. A rewrite that changes a number, a negation, a formula or a citation is discarded. The final Markdown is formatted with [prettier](https://prettier.io/).
- Each user picks the OpenAI models and the reasoning effort for the glossary, the translation, the polishing and the summary in the Home tab (by default `gpt-6.1-terra` for the glossary and the translation, `gpt-6.1-sol` for the polishing and the summary, all with the high effort).
- Papers translated once are stored and returned immediately next time.
- With `--no-cache`, nothing is kept on the server.

## Usage

### Sending a paper

Invite the bot to a channel and mention it with a paper.

```
@Paper2MD-Translator
https://arxiv.org/abs/2304.08485
```

Supported sources:

- an attached PDF
- a PDF URL (such as `https://example.com/paper.pdf`)
- arXiv links (`/abs/`, `/pdf/`, `/html/`)
- Hugging Face Papers and alphaXiv links (handled as arXiv papers)
- OpenReview links (`https://openreview.net/forum?id=...`)
- ACL Anthology links (such as `https://aclanthology.org/2024.acl-long.1/`)

If a long URL is not recognized, wrap it in backticks (`` ` ``).

Before the first request, register your OpenAI API key in the bot's Home tab. If you also register a Gyazo access token, the posted Markdown links its figures to Gyazo image URLs; without it, the Markdown inside the ZIP still shows the figures.

### Uploading the figures to Gyazo

Once a Gyazo access token is registered, the Home tab shows the checkbox "図表を Gyazo にアップロードする" (upload the figures to Gyazo) below it, on by default.

- On: the figures of a translation go to your Gyazo, and the posted Markdown links them by their Gyazo URLs. For a paper that was translated before, the stored Gyazo version is posted; when there is none, the figures are uploaded to your Gyazo for that post (that version is not stored).
- Off: Gyazo is not used. The posted Markdown links the figures inside the ZIP, also for papers that were translated before.

The "サーバーの既定値に戻す" (reset) button resets the models only, not this checkbox.

### Stopping a translation

The bot's first message of a translation ("🤖 … で処理します。") has a "中断する" (stop) button. Only the user who asked for the translation can press it; anyone else gets a note only they can see. After the confirmation, the job stops:

- while it waits for its turn: at once;
- during the layout analysis (Dolphin): when the page being analysed is done, and the GPU model is unloaded;
- during OpenAI requests: at once, dropping the requests in flight.

Nothing is cached and the intermediate files are removed. What OpenAI has already processed is still billed.

### API cost

The thread ends with the OpenAI API cost of the request in yen, by task (translation, polishing, summary).

- The cost is computed from the token counts in OpenAI's responses and the model prices: tokens read from and written to the prompt cache and reasoning tokens at their own rates, including the surcharge for requests over 272K input tokens.
- Dollars are converted with the day's European Central Bank reference rate (from frankfurter.dev), or 158 yen per dollar when it cannot be fetched; `USD_JPY_RATE` sets a fixed rate.
- When a job is stopped or fails, the cost up to that point is posted; requests that were in flight are not included.
- A paper answered from the cache costs 0 yen.
- The amount can differ slightly from OpenAI's invoice (rounding, exchange rate).

### Choosing the models

In the Home tab, under "モデル" (models), pick the model and the reasoning effort for the glossary (訳語表), the translation (翻訳), the polishing (推敲) and the summary (要約). Two checkboxes under 推敲 switch the two polishing passes: "ブロックごとに推敲する" (every block; off by default) and "Markdown 全体を推敲する" (the whole Markdown; on by default). With both unchecked, nothing is polished. By default the models are `gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.1-sol` and `gpt-6-astra` (`OPENAI_MODEL_CHOICES`), shown with their prices, and the effort menu offers only the values the chosen model accepts (`gpt-6.1-sol` and `gpt-6-astra` have no `none`). When you switch to a model that does not accept your effort, the closest accepted effort is used and the Home tab says so. Without a choice, the server defaults are used.

A higher effort can improve formulas and long sentences, but takes longer and costs more. When it starts, the bot posts the models it uses as a list in the thread.

### The term list

The glossary can follow the lab's term list (a TSV file). The list was built from textbooks, so it is not part of this repository (`references/` is ignored by git). By default the bot reads `references/AI・機械学習・統計学・NLP・CV・音声_用語と表記の一覧.tsv`; `TERM_LIST` points to another file. Without the file, the glossary is made without the list.

The list has about 10,000 rows, too many to send with every paper, so the glossary request only gets these rows:

1. Rows whose English appears in the paper, also only once. Case, hyphens and plural endings are ignored, except for acronyms (`IN`, `IS`), which match only in capitals, so the words "in" and "is" do not.
2. Not the notation rows (a paper keeps its own notation) or the code names (`toメソッド`, `q_proj`), which are written in English.
3. A row that only one book has is not found through a single ordinary word (position, section): such a row usually gives the word the sense of one field, like 出現位置 for position.
4. Each row is sent as "English: rendering［category, number of books with the term］"; for 1706.03762 that is 273 rows. When one English word has several rows of the same category (accuracy as 正解率 and 精度), the first sentence of each definition is added.

The model takes the list's rendering for the terms that have the same meaning in the paper, and picks the row of the paper's field when one English word has rows of several fields (sequence as 系列 or 数列). Terms that are not in the list are rendered as before. When the model gives a listed term a rendering that the list does not have, the bot logs it and saves the list's renderings next to it in `glossary.json`.

The variants column also holds expressions that are only close to a term (`compatibility function` in the row of attention score). A row found only through such a variant is marked as such, and the model is told to take its rendering only when the paper's term means the same thing. A name the paper gives in quotes or as a section heading of two words or more (`Scaled Dot-Product Attention`) is never taken as a variant, so the paper's own names are not replaced by other words of the list.

Names of methods, models, components and metrics that are not in the list (Scaled Dot-Product Attention) stay in English: the glossary and translation prompts say so, and the polishing does not translate English terms. A phrase that contains a listed term and otherwise plain words uses the list's rendering for that part (sinusoidal positional encoding → 正弦波の位置符号); a phrase with another unlisted technical term stays in English as a whole (recurrent layer), so English and Japanese are not mixed in one term. When the paper writes a listed term in a short form (attention for attention mechanism), the row says so and the short form takes the list's rendering too. Plain words such as model, position or stack are left out of the glossary, even when the list has them, and translated as usual. Add a row to the list for a term that should always be written in Japanese.

A row whose evidence column says ★研究室の方針で追加 is a rendering the lab decided; it is sent with 研究室の方針 instead of a number of books (development set → 開発データ, validation split → 検証データ, training set → 学習データ, and so on).

The glossary has at most 300 terms. Each translation and polishing request gets the glossary terms whose English appears in its block. The list is read when the bot starts and read again for the next paper when the file changes.

### Polishing with yomiyasu

[yomiyasu](https://github.com/nanaism/yomiyasu) (v2.5.0, MIT, in `yomiyasu/`) is a skill that rewrites AI-written Japanese into natural Japanese without changing what it says. The polishing model gets its `SKILL.md` and its references for papers as instructions, and works in yomiyasu's "body only" mode. There are two passes, switched in the Home tab:

1. Every block (off by default): each translated paragraph, list, caption and footnote is rewritten right after the translation, with one request per block.
2. The whole Markdown (on by default): the assembled paper is sent in a few chunks of up to about 16,000 characters (30,000 after the block pass), and the model returns only the paragraphs it rewrites. It polishes each paragraph and also what only shows across paragraphs (the same phrasing or sentence ending in a row, unclear connections between paragraphs); after the block pass, only the latter.

Formulas, code, citations, URLs, figure tags, paragraph names and figure / table numbers are replaced by placeholders before a text is sent, so they cannot change, and the terms of the glossary are kept. A rewrite is used only if every placeholder comes back, its length stays close to the original, and yomiyasu's own check (`yomiyasu_diff.py`, which calls no model) finds no meaning risk such as a changed number or negation; otherwise the translation is kept as it was.

Every polishing request carries yomiyasu's instructions (about 42,000 tokens). The whole-Markdown pass needs one request for a paper like 1706.03762 (15 pages), while the block pass needs one per block (37 for that paper), which is why it is off by default. If polishing fails, the bot uses the unpolished translation and says so in the thread.

### Papers that were already translated

The summary and translation of every paper are stored and shared by all users, so a paper translated once is returned immediately.

- arXiv papers are stored per version. `/abs/`, `/pdf/` and `/html/` links to the same version are the same paper.
- A link without a version (such as `https://arxiv.org/abs/2408.03326`) means the latest version when the request arrives. If v2 was translated before and v3 has been published since, v3 is translated; the v2 result is still returned for `https://arxiv.org/abs/2408.03326v2`.
- Other PDFs are identified by the SHA-256 of the file: the same file hits under any name, while a different file with the same name, or a changed file behind the same URL, is translated again.
- A stored result is returned whichever models you chose; the bot tells which models made it. Add `--refresh` (also `--force` or `再翻訳`) to translate the paper again with your models; the new result replaces the stored one.
- Results with failed segments are not stored.

### Keeping nothing on the server

Add `--no-cache` (also `no-cache`, `nocache`, `キャッシュなし` or `保存しない`).

```
@Paper2MD-Translator --no-cache
https://arxiv.org/abs/2304.08485
```

The PDF you sent, the intermediate files and the results exist only in a private temporary directory of the bot process (by default in RAM under `/dev/shm`, mode `0700`). The directory is deleted after the results are posted, also when processing fails, and the bot reports the deletion in the thread. The bot's log leaves out the paper's title, file name and words (a URL that could not be fetched may still appear in an error). A paper that is already stored is still answered from the cache; nothing new is stored. To translate it again, add `--refresh` as well; the stored result stays as it is.

Files posted to Slack, figures uploaded to Gyazo and OpenAI's own data handling are outside this scope. Requests to OpenAI are always sent with `store=false`.

## Setup

### Requirements

- Python 3.10 and [uv](https://docs.astral.sh/uv/)
- a GPU (Dolphin-v2 uses about 8 GB of GPU memory; it also runs on a CPU, but slowly)
- permission to add an app to the Slack workspace
- an OpenAI API key for each user (registered by each user in the Home tab)

### Installation

```bash
git clone https://github.com/Onely7/Paper2MD-Translator_for_slack.git
cd Paper2MD-Translator_for_slack
uv sync
uv run npm ci --prefix tools/prettier
```

`uv sync` also installs Node.js (the `nodejs-wheel` package) into `.venv`, and `npm ci` installs the pinned prettier into `tools/prettier/node_modules`. Without prettier, the Markdown is formatted with mdformat and a warning is logged.

### Dolphin-v2 model

```bash
uv run hf download ByteDance/Dolphin-v2 --local-dir models/Dolphin-v2
```

The model is about 7.5 GB. To keep it on another disk, set `DOLPHIN_MODEL_PATH` or symlink it to `models/Dolphin-v2`. It runs in bf16 on Ampere or newer GPUs and in fp16 on older ones such as the RTX 20 series.

Dolphin-v2 is distributed under the **Qwen Research License (non-commercial use only)**, not under the MIT license of Dolphin 1.x. See [Dolphin/LICENSE](./Dolphin/LICENSE).

### Creating the Slack app

1. In [Slack Your Apps](https://api.slack.com/apps), choose "Create New App" and then "From a manifest", and paste the contents of [slack_manifest.yaml](./slack_manifest.yaml).
2. Install the app to the workspace and copy the Bot User OAuth Token (`xoxb-...`) from "OAuth & Permissions".
3. Under "Basic Information" > App-Level Tokens, create a token (`xapp-...`) with the `connections:write` scope.

The manifest uses Socket Mode: the bot connects to Slack itself, so neither a public URL nor ngrok is needed.

### Environment variables

Create `.env` in the repository root with these two lines:

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
```

`.env` holds tokens; make it readable only by you with `chmod 600 .env`.

Optional settings (defaults in parentheses):

| Variable | Meaning |
| --- | --- |
| `TRANSLATION_MODEL` | Default OpenAI model for the translation, used for users who have not chosen one (`gpt-6.1-terra`) |
| `SUMMARY_MODEL` | Default OpenAI model for the summary (`gpt-6.1-sol`) |
| `TRANSLATION_REASONING_EFFORT` / `SUMMARY_REASONING_EFFORT` | Default reasoning effort (`high` / `high`). A value the model does not accept is replaced by the closest accepted one (e.g. `none` becomes `low` on `gpt-6.1-sol`) |
| `GLOSSARY_MODEL` / `GLOSSARY_REASONING_EFFORT` | Default model and reasoning effort that make the glossary (`gpt-6.1-terra` / `high`) |
| `POLISH_MODEL` / `POLISH_REASONING_EFFORT` | Default model and reasoning effort of the yomiyasu polishing (`gpt-6.1-sol` / `high`) |
| `POLISH_SEGMENTS` / `POLISH_DOCUMENT` | Whether the block pass / the whole-Markdown pass of the polishing runs for users who have not chosen (`0` / `1`) |
| `TRANSLATION_GLOSSARY` | `0` stops making the glossary of the paper's terms (`1`) |
| `TERM_LIST` | The lab's term list (TSV) used for the glossary; `off` uses none (`references/AI・機械学習・統計学・NLP・CV・音声_用語と表記の一覧.tsv`) |
| `OPENAI_MODEL_CHOICES` | Models offered in the Home tab, comma separated (`gpt-6-luna,gpt-6.1-terra,gpt-6.1-sol,gpt-6-astra`) |
| `TRANSLATION_IMAGE_DETAIL` | Image detail of the page crops sent to the model (`auto`) |
| `OPENAI_MAX_CONCURRENCY` | Parallel OpenAI requests per paper (`8`) |
| `PAGE_RESOLUTION` | Longest side in pixels of the rendered PDF pages (`4000`) |
| `MAX_PDF_PAGES` / `MAX_PDF_MB` | Largest accepted PDF (`80` pages / `100` MB) |
| `MAX_CONCURRENT_JOBS` | Papers translated at the same time (`2`) |
| `USD_JPY_RATE` | Yen per dollar for the API cost; without it, the day's European Central Bank reference rate |
| `PAPER_CACHE_DIR` | Where translated papers are stored (`data/cache`) |
| `NO_CACHE_WORKDIR` | Where `--no-cache` creates its temporary directories (`/dev/shm`) |
| `KEY_DB_PATH` | SQLite file with the users' API keys and model choices (`user_keys.db`) |
| `DOLPHIN_MODEL_PATH` | Location of the Dolphin-v2 model (`models/Dolphin-v2`) |
| `DOLPHIN_DEVICE` / `DOLPHIN_DTYPE` | Device and precision for Dolphin-v2 (`cuda`; bf16 or fp16 depending on the GPU) |
| `DOLPHIN_MAX_BATCH_SIZE` | Elements read by Dolphin-v2 in parallel (`4`) |
| `DOLPHIN_KEEP_LOADED` | `1` keeps Dolphin-v2 on the GPU between requests (`0`) |
| `DOLPHIN_RECOGNIZE_LABELS` | `all` makes Dolphin-v2 read the text of every element like upstream. By default it reads only the title, headings and references, and only on pages without a text layer (scanned PDFs) |
| `LOG_LEVEL` | Log level (`INFO`) |

`PAPER_CACHE_DIR` gets one directory per paper (`arxiv-<id>v<version>/` or `sha256-<hash>/`) with the PDF, the summary, the Markdown, the figures, the intermediate files and a `manifest.json`. The directory is created with mode `0700`.

### Starting the bot

```bash
scripts/run_app.sh
```

The script loads `.env` and starts the bot. It prints a warning when other users can read `.env`. Run it inside tmux or similar to keep it running after you log out.

### HTTP mode with ngrok

Without Socket Mode, leave out `SLACK_APP_TOKEN` and put `SLACK_SIGNING_SECRET` (the Signing Secret from Basic Information), `PORT` and `NGROK_AUTHTOKEN` in `.env`. In the manifest, set `socket_mode_enabled: false` and set the Request URL of Event Subscriptions and Interactivity to `https://<your ngrok domain>/slack/events`.

```bash
scripts/run_app.sh    # terminal 1
scripts/run_ngrok.sh  # terminal 2, prints the Request URL
```

On ngrok's free plan the URL changes every time ngrok starts, and the Request URL in Slack has to be updated each time.

### Translating from the command line

The pipeline also runs without Slack. Set `OPENAI_API_KEY` and run:

```bash
OPENAI_API_KEY=sk-... uv run python -m paper2md translate https://arxiv.org/abs/2304.08485 --out results/llava
```

`--gyazo` (uses `GYAZO_ACCESS_TOKEN`), `--translation-model`, `--translation-effort`, `--glossary-model`, `--glossary-effort`, `--polish-model`, `--polish-effort`, `--polish-blocks` / `--no-polish-blocks`, `--polish-document` / `--no-polish-document`, `--glossary` / `--no-glossary`, `--term-list` (the term list, or `off`), `--summary-model` and `--summary-effort` are also available. At the end, the OpenAI API cost is printed.

### Managing the cache

```bash
uv run python -m paper2md cache list                      # stored papers
uv run python -m paper2md cache show arxiv:2408.03326v3   # details of one entry
uv run python -m paper2md cache remove arxiv:2408.03326v3 # delete one entry
uv run python -m paper2md cache prune --older-than 180    # delete results stored more than 180 days ago
```

## How it works

1. The bot finds the paper in the message and looks it up in the cache (arXiv papers by their latest version, other PDFs by SHA-256).
2. If it is not stored, the PDF pages are rendered to images and Dolphin-v2 finds the position and reading order of figures, headings, paragraphs and other elements. A "page header" that is set in a heading font and appears on that page only (such as an appendix title at the top of a page) is kept as a heading.
3. The title, headings and reference list are copied from the text embedded in the PDF. For a scanned PDF without text, the text read by Dolphin-v2 is used, and failing that the model transcribes them without translating.
4. The model chosen for the glossary makes a glossary of the paper's main terms from its text, with the rows of the term list found in the paper.
5. The paragraphs are translated by an OpenAI model from the regions cropped out of the pages. Each request also carries the paper title, the section heading, the glossary terms found in that region and the text the PDF holds for it. The prompts are in `paper2md/prompt_texts/`.
6. If switched on, each translated block is polished with yomiyasu.
7. The parts are assembled into Markdown, with formulas normalized to `$...$` and `$$` blocks (the underscore of a name such as `step\_num` stays escaped).
8. If switched on (the default), the whole Markdown is polished with yomiyasu. Spaces are put between Japanese and English words, numbers, code and math, section references are written as Section 3.2.2, and prettier formats the file.
9. The paper is summarized with the terms and spellings of the translated Markdown only, posted to Slack and stored.

## Repository layout

| Path | Contents |
| --- | --- |
| `app.py` | Starts the bot (Socket Mode or HTTP mode) |
| `bot/` | Slack events, request handling, the Home tab, API key storage |
| `paper2md/` | The translation pipeline (PDF and its text, Dolphin-v2, layout, models, prompts, translation, Markdown, cache, CLI) |
| `paper2md/prompt_texts/` | The Japanese prompts for the glossary, the translation, the polishing and the summary |
| `references/` | The lab's term list (ignored by git) |
| `Dolphin/` | Code of [bytedance/Dolphin](https://github.com/bytedance/Dolphin) at commit `befa5da`; only import lines are changed |
| `yomiyasu/` | `skills/yomiyasu` of [nanaism/yomiyasu](https://github.com/nanaism/yomiyasu) v2.5.0 (commit `29d67cc`, MIT), unchanged |
| `tools/prettier/` | The pinned prettier version (`npm ci --prefix tools/prettier`) |
| `scripts/` | Start-up and ngrok scripts |
| `tests/` | Unit tests |
| `slack_manifest.yaml` | Slack app manifest |
| `docs/description.pdf` | Guide for users, posted when the bot is invited to a channel |

## Development

```bash
uv run python -m unittest discover -s tests -t .  # needs neither a GPU nor the network
uv run ruff check .
uv run ruff format --check .
```

## Citation

```bibtex
@software{
  author = {Daiki Shiono},
  month = {Nov},
  title = {{Paper2MD-Translator}},
  url = {https://github.com/Onely7/Paper2MD-Translator_for_slack},
  version = {0.2.0},
  year = {2026}
}
```

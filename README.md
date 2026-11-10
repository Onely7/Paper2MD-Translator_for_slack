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
- Each user picks the OpenAI models and the reasoning effort for the translation and the summary in the Home tab (by default `gpt-6.1-terra` for the translation and `gpt-6.1-sol` for the summary).
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

### Choosing the models

In the Home tab, under "モデル" (models), pick the model and the reasoning effort for the translation and for the summary. By default the models are `gpt-6-luna`, `gpt-6.1-terra`, `gpt-6.1-sol` and `gpt-6-astra` (`OPENAI_MODEL_CHOICES`), shown with their prices, and the effort menu offers only the values the chosen model accepts (`gpt-6.1-sol` and `gpt-6-astra` have no `none`). When you switch to a model that does not accept your effort, the closest accepted effort is used and the Home tab says so. Without a choice, the server defaults are used.

A higher effort can improve formulas and long sentences, but takes longer and costs more. The bot posts the models it uses when it starts translating.

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

The PDF you sent, the intermediate files and the results exist only in a private temporary directory of the bot process (by default in RAM under `/dev/shm`, mode `0700`). The directory is deleted after the results are posted, also when processing fails, and the bot reports the deletion in the thread. A paper that is already stored is still answered from the cache; nothing new is stored. To translate it again, add `--refresh` as well; the stored result stays as it is.

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
```

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
| `TRANSLATION_REASONING_EFFORT` / `SUMMARY_REASONING_EFFORT` | Default reasoning effort (`none` / `medium`). A value the model does not accept is replaced by the closest accepted one (e.g. `none` becomes `low` on `gpt-6.1-sol`) |
| `OPENAI_MODEL_CHOICES` | Models offered in the Home tab, comma separated (`gpt-6-luna,gpt-6.1-terra,gpt-6.1-sol,gpt-6-astra`) |
| `TRANSLATION_IMAGE_DETAIL` | Image detail of the page crops sent to the model (`auto`) |
| `OPENAI_MAX_CONCURRENCY` | Parallel OpenAI requests per paper (`8`) |
| `PAGE_RESOLUTION` | Longest side in pixels of the rendered PDF pages (`4000`) |
| `MAX_PDF_PAGES` / `MAX_PDF_MB` | Largest accepted PDF (`80` pages / `100` MB) |
| `MAX_CONCURRENT_JOBS` | Papers translated at the same time (`2`) |
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

`--gyazo` (uses `GYAZO_ACCESS_TOKEN`), `--translation-model`, `--translation-effort`, `--summary-model` and `--summary-effort` are also available.

### Managing the cache

```bash
uv run python -m paper2md cache list                      # stored papers
uv run python -m paper2md cache show arxiv:2408.03326v3   # details of one entry
uv run python -m paper2md cache remove arxiv:2408.03326v3 # delete one entry
uv run python -m paper2md cache prune --older-than 180    # delete results stored more than 180 days ago
```

## How it works

1. The bot finds the paper in the message and looks it up in the cache (arXiv papers by their latest version, other PDFs by SHA-256).
2. If it is not stored, the PDF pages are rendered to images and Dolphin-v2 finds the position and reading order of figures, headings, paragraphs and other elements.
3. The title, headings and reference list are copied from the text embedded in the PDF. For a scanned PDF without text, the text read by Dolphin-v2 is used, and failing that the model transcribes them without translating.
4. The paragraphs are translated by an OpenAI model from the regions cropped out of the pages; each request also carries the paper title and the section heading. The prompts are in `paper2md/prompt_texts/`.
5. The parts are assembled into Markdown, with formulas normalized to `$...$` and `$$` blocks, summarized, posted to Slack and stored.

## Repository layout

| Path | Contents |
| --- | --- |
| `app.py` | Starts the bot (Socket Mode or HTTP mode) |
| `bot/` | Slack events, request handling, the Home tab, API key storage |
| `paper2md/` | The translation pipeline (PDF and its text, Dolphin-v2, layout, models, prompts, translation, Markdown, cache, CLI) |
| `paper2md/prompt_texts/` | The Japanese prompts for the translation and the summary |
| `Dolphin/` | Code of [bytedance/Dolphin](https://github.com/bytedance/Dolphin) at commit `befa5da`; only import lines are changed |
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

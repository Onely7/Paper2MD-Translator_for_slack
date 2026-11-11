<div align="center">

# Paper2MD-Translator

**論文の PDF や URL を Slack に送ると、日本語に翻訳した Markdown と要約を返す Slack ボット**

[English](../README.md) | 日本語

<img src="../images/Paper2MD-Translator_logo.png" alt="paper2md-translator" width=200px>

</div>

## できること

- 論文を送ると、日本語の要約、翻訳した Markdown、Markdown と図表をまとめた ZIP、図表の画像をスレッドに返します。
- ページ内の図表・見出し・本文・数式・参考文献の位置は [Dolphin-v2](https://github.com/bytedance/Dolphin) で解析し、本文は OpenAI のモデルでページの画像から翻訳します。数式は LaTeX で書きます。
- タイトル、見出し、パラグラフ名、参考文献は英語のまま残します。タイトル、見出し、参考文献は PDF に書かれている文字をそのまま写します。
- 英字、数字、コード、数式と日本語の間には空白を入れ（3600万 のような万・億・兆の前には入れません）、節の参照は Section 3.2.2 の形にそろえます（参考文献の一覧は変えません）。要約には、翻訳した Markdown にある用語と表記だけを使います。
- 翻訳の前に、論文の主な用語の訳語表を作ります。ブロックごとに別のリクエストで訳しても、同じ用語は同じ訳語になります。研究室の用語と表記の一覧を置いておくと、一覧にある語は一覧の訳語にそろえるので、どの論文でも同じ訳語になります。各ブロックは、その部分の PDF に埋め込まれた文字と一緒に送るので、記号、数値、固有名詞を正しく写せます。
- 翻訳した Markdown の全体を [yomiyasu](https://github.com/nanaism/yomiyasu) で推敲します。Home タブでは、ブロックごとの推敲も選べます。数値、否定、数式、引用番号が変わる書き直しは使いません。最後に、Markdown を [prettier](https://prettier.io/) で整形します。
- 訳語表、翻訳、推敲、要約に使うモデルと Reasoning Effort は、利用者がそれぞれ Home タブで選べます。既定は、訳語表と翻訳が `gpt-6.1-terra`、推敲と要約が `gpt-6.1-sol` で、Reasoning Effort はどれも high です。
- 一度翻訳した論文は保存しておき、次からはすぐに返します。
- `--no-cache` を付けると、サーバーにデータを残さずに処理します。

## 使い方

### 論文を送る

Bot をチャンネルに招待し、メンション付きで論文を送ってください。

```
@Paper2MD-Translator
https://arxiv.org/abs/2304.08485
```

次の形式に対応しています。

- PDF の添付
- PDF の URL（`https://example.com/paper.pdf` など）
- arXiv のリンク（`/abs/`、`/pdf/`、`/html/`）
- Hugging Face Papers と alphaXiv のリンク（arXiv の論文として扱います）
- OpenReview のリンク（`https://openreview.net/forum?id=...`）
- ACL Anthology のリンク（`https://aclanthology.org/2024.acl-long.1/` など）

長い URL がうまく読み取られないときは、URL をバッククォート（`` ` ``）で囲んで送ってください。

使う前に、Paper2MD-Translator の Home タブで OpenAI の API Key を登録してください。Gyazo のアクセストークンも登録すると、投稿される Markdown の図表が Gyazo の画像 URL になります。登録しない場合も、ZIP の中の Markdown では図表が表示されます。

### 図表を Gyazo にアップロードするか

Gyazo のアクセストークンを登録すると、Home タブのトークンの下に「図表を Gyazo にアップロードする」のチェックが出ます（既定はオン）。

- オン: 翻訳するときに、図表を自分の Gyazo にアップロードし、投稿する Markdown の図表を Gyazo の画像 URL にします。保存済みの論文を返すときは、保存してある、Gyazo の画像 URL 入りの Markdown を投稿します。それが保存されていなければ、その場で図表を自分の Gyazo にアップロードします（このとき作った Markdown は保存しません）。
- オフ: Gyazo を使いません。投稿する Markdown の図表は、ZIP の中の画像を指します。保存済みの論文を返すときも同じです。

「サーバーの既定値に戻す」ボタンはモデルの設定だけを戻し、このチェックは変えません。

### 処理を中断する

処理を始めるときに投稿する「🤖 … で処理します。」のメッセージに、「中断する」ボタンが付きます。押せるのは、その論文の翻訳を依頼した人だけです。ほかの人が押すと、押した人にだけ見えるメッセージで断ります。確認の画面で「中断する」を選ぶと、次のところで処理が止まります。

- 順番待ちのあいだ: すぐに止まります。
- ページ内要素の解析（Dolphin）のあいだ: 解析中のページが終わったところで止まります。GPU のモデルは解放します。
- OpenAI へのリクエストのあいだ: すぐに止まります。送信済みのリクエストも打ち切ります。

止めた結果は保存せず、途中のファイルは消します。OpenAI がすでに処理した分の料金は戻りません。

### API の料金

スレッドの最後に、その依頼でかかった OpenAI API の料金を、翻訳・推敲・要約ごとに円で投稿します。

- 料金は、OpenAI の応答にあるトークン数とモデルの単価から計算します。キャッシュから読んだ分、キャッシュに書いた分、推論の分を、それぞれの単価で数えます。272K トークンを超える長いリクエストの割増しも含めます。
- ドルから円への換算には、その日の欧州中央銀行の参照レート（frankfurter.dev から取得）を使います。取得できないときは 1 ドル = 158 円で換算します。`USD_JPY_RATE` を設定すると、その値を使います。
- 中断したときやエラーで止まったときは、止まるまでの料金を投稿します。止まったときに処理中だったリクエストの分は含みません。
- 保存済みの論文を返したときは、0 円と投稿します。
- 実際の請求額とは、端数や為替レートの違いでずれることがあります。

### モデルを選ぶ

Home タブの「モデル」で、訳語表、翻訳、推敲、要約に使うモデルと Reasoning Effort を選べます。推敲の下にある 2 つのチェックで、推敲の 2 つの段階をそれぞれ選べます。「ブロックごとに推敲する」は既定でオフ、「Markdown 全体を推敲する」は既定でオンです。どちらも外すと推敲しません。モデルの選択肢は、既定では `gpt-6-luna`、`gpt-6.1-terra`、`gpt-6.1-sol`、`gpt-6-astra` の 4 つで（`OPENAI_MODEL_CHOICES` で変えられます）、料金と一緒に表示します。Reasoning Effort の選択肢には、選んだモデルが受け付ける値だけが出ます（`gpt-6.1-sol` と `gpt-6-astra` では `none` を選べません）。選んでいた Reasoning Effort を受け付けないモデルに変えたときは、受け付ける値のうちいちばん近い値にして、そのことを Home タブに表示します。何も選んでいない場合は、サーバーの既定値を使います。

Reasoning Effort を上げると数式や長い文の翻訳がよくなることがありますが、時間と料金が増えます。処理を始めるときに、使うモデルを箇条書きでスレッドに投稿します。

### 用語と表記の一覧

訳語表を作るときに、研究室の用語と表記の一覧（TSV）を使えます。一覧は書籍をもとに作ったものなので、このリポジトリには入れていません（`references/` は git の管理から外しています）。既定では `references/AI・機械学習・統計学・NLP・CV・音声_用語と表記の一覧.tsv` を読みます。別の場所は `TERM_LIST` で指定できます。ファイルがなければ、一覧を使わずに訳語表を作ります。

一覧は 1 万行ほどあるので、そのままモデルに渡さず、次のように絞ってから訳語表のリクエストに添えます。

1. 論文の英文に出てくる語の行を拾います。1 回しか出てこない語も拾います。大文字と小文字、ハイフン、複数形の違いは区別しません。略語（`IN`、`IS` など）は大文字のときだけ拾うので、英文の「in」や「is」には当たりません。
2. 記法・記号の行と、コードの名前の行（`toメソッド`、`q_proj` など）は使いません。論文の数式は論文の記号のまま書き、コードは英語のまま書くためです。
3. 1 冊の書籍にしか載っていない行は、普通の 1 語（position、section など）だけでは拾いません。position の「出現位置」のように、ある分野だけの訳語であることが多いためです。
4. 拾った行を「英語: 訳語［区分、その語が載っている書籍の数］」の形で並べます。1706.03762 では 273 行になりました。同じ英語で同じ区分の行が 2 つ以上あるとき（accuracy の正解率と精度など）は、定義の最初の 1 文も添えます。

モデルは、論文での意味が同じ語には一覧の訳語をそのまま使い、同じ英語に別の分野の行が並ぶとき（sequence の系列と数列など）は、論文に合う行を選びます。一覧にない語の訳語は、これまでどおりモデルが決めます。一覧にある語に一覧にない訳語を当てたときはログに残し、`glossary.json` には一覧の訳語も並べて保存します。

一覧の揺れの欄には、意味の近い別の語も入っています（`compatibility function` が attention score の行にあるなど）。そこで、一覧の英語そのものではなく揺れの欄の語だけが論文に出てくる行は、そのことを書き添えて渡し、論文の語が一覧の語と同じものを指すときだけ一覧の訳語を使うよう指示しています。論文が引用符で囲んで名付けた語と、2 語以上の節の見出しになっている語（`Scaled Dot-Product Attention` など）は、揺れの欄の語としては拾いません。論文が付けた名前を、一覧の別の語に置き換えないためです。

一覧にない語のうち、手法、モデル、構成要素、指標などの名前（Scaled Dot-Product Attention など）は、日本語に訳さず英語のまま書きます。訳語表と翻訳のプロンプトでそう指示し、推敲でも英語で書いた用語を日本語に直させません。一覧の語を含む言い回しは、残りが普通の語なら、一覧の語の部分を一覧の訳語にします（sinusoidal positional encoding → 正弦波の位置符号）。残りに一覧にない専門用語があるときは、英語と日本語を混ぜず、全体を英語のまま書きます（recurrent layer）。論文が一覧の語を略して書いているとき（attention mechanism を attention と書くなど）は、そのことを添えて渡し、略した形にも一覧の訳語を使わせます。model や position、stack のような普通の語は、一覧にあっても訳語表に入れず、翻訳のときに普通に訳します。どの論文でも日本語で書きたい語は、一覧に行を足してください。

採用根拠の欄に「★研究室の方針で追加」と書いた行は、研究室で決めた訳語として、書籍の数の代わりに「研究室の方針」と書いて渡します（development set → 開発データ、validation split → 検証データ、training set → 学習データ など）。

訳語表は多くても 300 語です。翻訳と推敲のリクエストには、訳語表のうち、そのブロックの英文に出てくる語だけを添えます。一覧はボットの起動時に読み込み、ファイルを書き換えると、次の論文を処理するときに読み直します。

### yomiyasu による推敲

[yomiyasu](https://github.com/nanaism/yomiyasu)（v2.5.0、MIT ライセンス、`yomiyasu/`）は、AI が書いた日本語を、意味を変えずに自然な日本語に直すスキルです。推敲に使うモデルには yomiyasu の `SKILL.md` と論文向けの参照ファイルを指示として渡し、yomiyasu の「本文のみの指定」で書き直させます。推敲には次の 2 つの段階があり、Home タブでそれぞれ選べます。

1. ブロックごと（既定ではオフ）: 翻訳が済んだ段落、箇条書き、図表の説明、脚注を、ブロックごとに 1 回のリクエストで書き直します。
2. Markdown 全体（既定ではオン）: まとめた論文を 1 万 6000 字ほど（ブロックごとの推敲のあとは 3 万字ほど）ずつに分けて送り、モデルは書き直した段落だけを返します。段落ごとの書き直しに加えて、段落をまたいで分かる問題（同じ言い回しや文末が続く、段落のつながりが分かりにくいなど）も直します。ブロックごとの推敲のあとは、段落をまたぐ問題だけを直します。

数式、コード、引用番号、URL、図の画像、パラグラフ名、図表の番号は記号に置き換えてから送るので、書き換わりません。訳語表の訳語も変えさせません。書き直した文章を使うのは、置き換えた記号がすべて残り、長さが元から大きく変わらず、yomiyasu 自身の検査（モデルを呼ばない `yomiyasu_diff.py`）で数値や否定などの意味が変わるおそれが見つからないときだけです。そうでなければ、訳文をそのまま使います。

推敲のリクエストには毎回 yomiyasu の指示（約 4 万 2000 トークン）が付きます。1706.03762（15 ページ）の場合、Markdown 全体の推敲は 1 回のリクエストで済みますが、ブロックごとの推敲は 37 回になります。そのため、ブロックごとの推敲は既定でオフにしています。推敲に失敗したときは推敲していない訳文を使い、そのことをスレッドに投稿します。

### 翻訳済みの論文

一度翻訳した論文は要約と翻訳を保存しておき、次からはすぐに返します。保存した結果は利用者全員で共有します。

- arXiv の論文はバージョンごとに保存します。同じバージョンなら `/abs/`、`/pdf/`、`/html/` のどのリンクでも同じ論文として扱います。
- バージョンを指定しないリンク（`https://arxiv.org/abs/2408.03326` など）は、送った時点の最新版を翻訳します。以前に v2 を翻訳していても、その後 v3 が公開されていれば v3 を翻訳します。v2 の結果は `https://arxiv.org/abs/2408.03326v2` で引き続き返します。
- ほかの PDF はファイルの中身（SHA-256）で同じ論文かを判定します。名前が違っても中身が同じなら保存済みの結果を返し、同じ名前や同じ URL でも中身が違えば翻訳し直します。
- 保存済みの結果は、利用者が選んでいるモデルにかかわらず返し、その結果を作ったモデルをスレッドで知らせます。今のモデルで翻訳し直すときは `--refresh` を付けてください（`--force`、`再翻訳` でも同じです）。新しい結果で保存済みの結果を置き換えます。
- 一部の翻訳に失敗した結果は保存しません。

### サーバーにデータを残さない

`--no-cache` を付けて送ってください（`no-cache`、`nocache`、`キャッシュなし`、`保存しない` でも同じです）。

```
@Paper2MD-Translator --no-cache
https://arxiv.org/abs/2304.08485
```

送った PDF、途中のファイル、結果は、サーバー上のプロセス専用の一時ディレクトリ（既定は RAM 上の `/dev/shm`、権限 `0700`）だけで扱います。結果をスレッドに投稿したあと、失敗したときも含めてこのディレクトリを削除し、削除したことをスレッドに投稿します。ボットのログにも、論文の題名、ファイル名、本文の語は書きません（取得に失敗したときは、その URL がエラーとして残ることがあります）。すでに保存されている論文は、このモードでも保存済みの結果を返します。新しく保存するものはありません。翻訳し直すときは `--refresh` も付けてください。保存済みの結果はそのまま残ります。

Slack に投稿したファイル、Gyazo にアップロードした図表、OpenAI 側でのデータの扱いは対象外です。OpenAI へのリクエストは常に `store=false` で送ります。

## セットアップ

### 必要なもの

- Python 3.10 と [uv](https://docs.astral.sh/uv/)
- GPU（Dolphin-v2 は GPU メモリを約 8 GB 使います。CPU でも動きますが、遅くなります）
- Slack ワークスペースにアプリを追加する権限
- 利用者それぞれの OpenAI API Key（各自が Home タブで登録します）

### インストール

```bash
git clone https://github.com/Onely7/Paper2MD-Translator_for_slack.git
cd Paper2MD-Translator_for_slack
uv sync
uv run npm ci --prefix tools/prettier
```

`uv sync` は Node.js（`nodejs-wheel` パッケージ）も `.venv` に入れます。`npm ci` は、バージョンを固定した prettier を `tools/prettier/node_modules` に入れます。prettier がないときは、mdformat で整形し、ログに警告を出します。

### Dolphin-v2 のモデル

```bash
uv run hf download ByteDance/Dolphin-v2 --local-dir models/Dolphin-v2
```

モデルの大きさは約 7.5 GB です。別のディスクに置く場合は、`DOLPHIN_MODEL_PATH` でその場所を指定するか、`models/Dolphin-v2` にシンボリックリンクを張ってください。Ampere 以降の GPU では bf16、RTX 20 系のような古い GPU では fp16 で動かします。

Dolphin-v2 のライセンスは Dolphin 1.x の MIT ライセンスではなく **Qwen Research License（非商用利用のみ）** です。詳しくは [Dolphin/LICENSE](../Dolphin/LICENSE) を確認してください。

### Slack アプリの作成

1. [Slack の Your Apps](https://api.slack.com/apps) で「Create New App」から「From a manifest」を選び、[slack_manifest.yaml](../slack_manifest.yaml) の中身を貼り付けます。
2. アプリをワークスペースにインストールし、「OAuth & Permissions」にある Bot User OAuth Token（`xoxb-...`）を控えます。
3. 「Basic Information」の App-Level Tokens で、`connections:write` のスコープを付けたトークン（`xapp-...`）を作ります。

このマニフェストは Socket Mode を使います。Socket Mode ではボットのほうから Slack に接続するので、公開 URL も ngrok も要りません。

### 環境変数

リポジトリの直下に `.env` を作り、次の 2 つを書きます。

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
```

`.env` にはトークンが入るので、`chmod 600 .env` で自分だけが読めるようにしてください。

ほかに次の値を設定できます。括弧内は既定値です。

| 変数 | 内容 |
| --- | --- |
| `TRANSLATION_MODEL` | 翻訳に使う OpenAI のモデルの既定値。Home タブでモデルを選んでいない利用者に使います（`gpt-6.1-terra`） |
| `SUMMARY_MODEL` | 要約に使う OpenAI のモデルの既定値（`gpt-6.1-sol`） |
| `TRANSLATION_REASONING_EFFORT` / `SUMMARY_REASONING_EFFORT` | Reasoning Effort の既定値（`high` / `high`）。モデルが受け付けない値は、受け付ける値のうちいちばん近い値に置き換えます（例: `gpt-6.1-sol` では `none` を `low` にします） |
| `GLOSSARY_MODEL` / `GLOSSARY_REASONING_EFFORT` | 訳語表を作るモデルと Reasoning Effort の既定値（`gpt-6.1-terra` / `high`） |
| `POLISH_MODEL` / `POLISH_REASONING_EFFORT` | yomiyasu による推敲のモデルと Reasoning Effort の既定値（`gpt-6.1-sol` / `high`） |
| `POLISH_SEGMENTS` / `POLISH_DOCUMENT` | ブロックごとの推敲と Markdown 全体の推敲を、選んでいない利用者に行うかどうか（`0` / `1`） |
| `TRANSLATION_GLOSSARY` | `0` にすると、論文の訳語表を作りません（`1`） |
| `TERM_LIST` | 訳語表に使う用語と表記の一覧（TSV）。`off` にすると使いません（`references/AI・機械学習・統計学・NLP・CV・音声_用語と表記の一覧.tsv`） |
| `OPENAI_MODEL_CHOICES` | Home タブの選択肢に出すモデル（カンマ区切り。`gpt-6-luna,gpt-6.1-terra,gpt-6.1-sol,gpt-6-astra`） |
| `TRANSLATION_IMAGE_DETAIL` | モデルに送るページ画像の detail（`auto`） |
| `OPENAI_MAX_CONCURRENCY` | 1 本の論文で同時に送る OpenAI へのリクエストの数（`8`） |
| `PAGE_RESOLUTION` | PDF のページを画像にするときの長辺のピクセル数（`4000`） |
| `MAX_PDF_PAGES` / `MAX_PDF_MB` | 受け付ける PDF のページ数と大きさの上限（`80` / `100`） |
| `MAX_CONCURRENT_JOBS` | 同時に翻訳する論文の数（`2`） |
| `USD_JPY_RATE` | API の料金を円にするときの、1 ドルあたりの円。設定しないと、その日の欧州中央銀行の参照レートを使います |
| `PAPER_CACHE_DIR` | 翻訳済みの論文の保存先（`data/cache`） |
| `NO_CACHE_WORKDIR` | `--no-cache` の一時ディレクトリを作る場所（`/dev/shm`） |
| `KEY_DB_PATH` | 利用者の API Key と選んだモデルを保存する SQLite のファイル（`user_keys.db`） |
| `DOLPHIN_MODEL_PATH` | Dolphin-v2 のモデルの場所（`models/Dolphin-v2`） |
| `DOLPHIN_DEVICE` / `DOLPHIN_DTYPE` | Dolphin-v2 を動かすデバイスと精度（`cuda`。精度は GPU に合わせて bf16 か fp16） |
| `DOLPHIN_MAX_BATCH_SIZE` | Dolphin-v2 が並べて読み取る要素の数（`4`） |
| `DOLPHIN_KEEP_LOADED` | `1` にすると、Dolphin-v2 をリクエストの合間も GPU に置いたままにします（`0`） |
| `DOLPHIN_RECOGNIZE_LABELS` | `all` にすると、上流と同じくすべての要素の文字を Dolphin-v2 で読み取ります（既定では、文字が埋め込まれていないページ（スキャンした PDF）でだけ、タイトル・見出し・参考文献を読み取ります） |
| `LOG_LEVEL` | ログの出力レベル（`INFO`） |

`PAPER_CACHE_DIR` には論文ごとのディレクトリ（`arxiv-<id>v<version>/` か `sha256-<hash>/`）を作り、PDF、要約、Markdown、図表、途中のファイル、`manifest.json` を保存します。ディレクトリは権限 `0700` で作ります。

### 起動

```bash
scripts/run_app.sh
```

`.env` を読み込んでボットを起動します。`.env` をほかの利用者も読める権限のときは警告を表示します。tmux などの中で起動しておくと、ログアウトしたあとも動き続けます。

### HTTP モードで動かす

Socket Mode を使わない場合は、`SLACK_APP_TOKEN` を書かずに、`SLACK_SIGNING_SECRET`（Basic Information の Signing Secret）、`PORT`、`NGROK_AUTHTOKEN` を `.env` に書きます。マニフェストは `socket_mode_enabled: false` にし、Event Subscriptions と Interactivity の Request URL を `https://<ngrok のドメイン>/slack/events` にします。

```bash
scripts/run_app.sh    # ターミナル 1
scripts/run_ngrok.sh  # ターミナル 2。Request URL を表示します
```

ngrok の無料プランでは起動のたびに URL が変わるので、そのたびに Slack 側の Request URL も変える必要があります。

### コマンドラインでの翻訳

Slack を通さずに翻訳することもできます。`OPENAI_API_KEY` を設定して実行してください。

```bash
OPENAI_API_KEY=sk-... uv run python -m paper2md translate https://arxiv.org/abs/2304.08485 --out results/llava
```

`--gyazo`（`GYAZO_ACCESS_TOKEN` を使います）、`--translation-model`、`--translation-effort`、`--glossary-model`、`--glossary-effort`、`--polish-model`、`--polish-effort`、`--polish-blocks` / `--no-polish-blocks`、`--polish-document` / `--no-polish-document`、`--glossary` / `--no-glossary`、`--term-list`（用語と表記の一覧。`off` で使いません）、`--summary-model`、`--summary-effort` も指定できます。終わると、OpenAI API の料金を表示します。

### キャッシュの管理

```bash
uv run python -m paper2md cache list                      # 保存済みの論文の一覧
uv run python -m paper2md cache show arxiv:2408.03326v3   # 1 件の詳細
uv run python -m paper2md cache remove arxiv:2408.03326v3 # 1 件の削除
uv run python -m paper2md cache prune --older-than 180    # 180 日より前に保存した結果の削除
```

## 処理の流れ

1. Slack のメッセージから論文を特定し、保存済みかどうかを調べます。arXiv は最新版のバージョンを、ほかの PDF は SHA-256 をキーにします。
2. 保存済みでなければ、PDF のページを画像にし、Dolphin-v2 で図表・見出し・本文などの位置と読む順番を解析します。「ページのヘッダー」と判定された要素のうち、見出しの書体で書かれ、そのページにしかないもの（ページの先頭にある付録の題名など）は、見出しとして扱います。
3. タイトル、見出し、参考文献は、PDF に埋め込まれた文字をそのまま写します。文字が埋め込まれていないスキャンした PDF では Dolphin-v2 が読み取った文字を使い、それもなければモデルに翻訳させずに書き写させます。
4. 訳語表に選んだモデルが、論文の文字から主な用語の訳語表を作ります。用語と表記の一覧があれば、論文に出てくる語の行も添えます。
5. 本文はページから切り出した画像のまま OpenAI のモデルに渡して翻訳します。リクエストには、論文のタイトル、節の見出し、訳語表のうちその部分に出てくる語、その部分の PDF に埋め込まれた文字も添えます。プロンプトは `paper2md/prompt_texts/` にあります。
6. ブロックごとの推敲を選んでいれば、翻訳したブロックをそれぞれ yomiyasu で推敲します。
7. 数式を `$...$` と `$$` のブロックにそろえて Markdown にまとめます（`step\_num` のような名前の `_` はエスケープしたまま残します）。
8. Markdown 全体の推敲を選んでいれば（既定）、全体を yomiyasu で推敲します。英字、数字、コード、数式と日本語の間に空白を入れ、節の参照を Section 3.2.2 の形にそろえてから、prettier で整形します。
9. 翻訳した Markdown にある用語と表記だけを使って要約を作り、Slack に投稿して保存します。

## ディレクトリ構成

| パス | 内容 |
| --- | --- |
| `app.py` | ボットの起動（Socket Mode と HTTP モード） |
| `bot/` | Slack のイベント、リクエストの処理、Home タブ、API Key の保存 |
| `paper2md/` | 翻訳の処理（PDF とその文字、Dolphin-v2、レイアウト、モデル、プロンプト、翻訳、Markdown、キャッシュ、CLI） |
| `paper2md/prompt_texts/` | 訳語表、翻訳、推敲、要約の日本語のプロンプト |
| `references/` | 研究室の用語と表記の一覧を置く場所（git の管理から外しています） |
| `Dolphin/` | [bytedance/Dolphin](https://github.com/bytedance/Dolphin) のコミット `befa5da` のコード。import の行だけを変えています |
| `yomiyasu/` | [nanaism/yomiyasu](https://github.com/nanaism/yomiyasu) v2.5.0（コミット `29d67cc`、MIT ライセンス）の `skills/yomiyasu`。変更していません |
| `tools/prettier/` | バージョンを固定した prettier（`npm ci --prefix tools/prettier` で入れます） |
| `scripts/` | 起動と ngrok のスクリプト |
| `tests/` | 単体テスト |
| `slack_manifest.yaml` | Slack アプリのマニフェスト |
| `docs/description.pdf` | Bot をチャンネルに招待したときに投稿する、利用者向けの説明 |

## 開発

```bash
uv run python -m unittest discover -s tests -t .  # GPU とネットワークは使いません
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

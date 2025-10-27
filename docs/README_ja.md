<div align="center">

# Paper2MD-Translator

**Slack 経由で、論文 PDF/URL → 日本語翻訳 Markdown ファイルを生成する**

[English](../README.md) | 日本語

</div>

<div align="center">
<img src="../images/Paper2MD-Translator_logo.png" alt="paper2md-translator" width=200px>
</div>

<div align="left">
このアプリを使用することで、論文 PDF もしくは 論文 PDF の URL から日本語 Markdown ファイルを生成すると同時に要約も生成します。

## 利用者(ユーザー)側のインストラクション

まず、Slack のユーザーインターフェース画面から、[description.pdf](../description.pdf) の説明に従って、初期設定を完了してください。<br>
次に、以下のように、Paper2MD-Translator Bot を利用したいチャンネルに招待して、`@Paper2MD-Translator`へのメンション付きで論文 URL/PDF を添付して送信することで、クエリが投入されます。

```
@Paper2MD-Translator
https://arxiv.org/abs/2304.08485
```

現在、対応している論文の形式は以下のものになります。

- 論文 PDF を直接添付
- `http://hoge.pdf`, `https://hoge.pdf` のような形式の URL
- arXiv リンク
  - e.g., `https://arxiv.org/abs/2304.08485`
  - e.g., `https://arxiv.org/pdf/2304.08485`


## 運用・管理者側のインストラクション

### 環境構築

```bash
$ git clone https://github.com/Onely7/Paper2MD-Translator_for_slack.git
$ cd Paper2MD-Translator_for_slack
```

#### 環境変数の指定

プロジェクトディレクトリ配下に `.env` ファイルを作成して、以下のフィールドを埋めてください。

```.env
export SLACK_SIGNING_SECRET="<70*****>"
export SLACK_BOT_TOKEN="<xo*****>"
export PORT="<8888>"
export NGROK_AUTHTOKEN="<2z*****>"
export SAVE_PATH="<./results>"
```

- [Slack の Your Apps のページ](https://api.slack.com/apps) の「Create New App」から、「Paper2MD-Translator」App を新しく作成し、「Settings」の「Basic Information」に `SLACK_SIGNING_SECRET` の情報が存在し、「Settings」の「Install App」に `SLACK_BOT_TOKEN` の情報が存在しています。
- `PORT` は Paper2MD-Translator を動作させるサーバーで空いているポートを指定してください。
- [ngrok のホームページ](https://ngrok.com/) からアカウントを作成することで、`NGROK_AUTHTOKEN` の情報が得られます。ngrok を活用することで、ローカルのポートを public URL と紐づけ、ローカルで動作している本アプリを外部からアクセスできるようにします。
- `SAVE_PATH` には、Paper2MD-Translator で処理された中間データと最終生成物 (英語論文が日本語翻訳された markdown ファイル) が保存されるパスを指定します。


#### Python 仮想環境作成

プロジェクトルートディレクトリに移動して、以下を実行してください。

```bash
$ cd Paper2MD-Translator_for_slack
$ uv python install 3.10.18
$ uv python pin 3.10.18
$ uv sync
```

#### 学習済みドキュメント解析モデルのダウンロード

Paper2MD-Translator は、論文ページ（画像）内の図・表とテキスト部分を切り分けるために、オープンソースツールの [Dolphin](https://github.com/bytedance/Dolphin) を使用しています。
このツールで使用される、ByteDance/Dolphin チームが提供する [学習済みモデル](https://huggingface.co/ByteDance/Dolphin-1.5) をあらかじめダウンロードしてください。

```bash
$ uv run huggingface-cli download ByteDance/Dolphin-1.5 --local-dir ./Dolphin/hf_model
```

### Paper2MD-Translator サーバーを立てる

#### 準備①: Slack のクエリを受け付けられる状態にする

ターミナル 1 では、Paper2MD-Translator サーバーを立ち上げます。<br>
また、ターミナル 2 では、ngrok を使用することで、Paper2MD-Translator サーバーのローカルポートを public URL と紐づけ、ローカルで動作している本アプリを外部からアクセスできるようにする ngrok サーバーを立ち上げます。<br>
以下に示すように、ターミナルを 2 つ展開し、同時に実行した状態にしておいてください。（tmux が使える場合は、tmux を使って常時 2 つのサーバーが立ち上がっている状態にすることをおすすめします。）

##### ターミナル 1

```bash
# Slack 側からのクエリ投入待ちの状態にする
$ /bin/bash app.sh
```

##### ターミナル 2

```bash
# ngrok により、ローカルポートを public URL と紐づける
$ /bin/bash run_ngrok.sh

## 以下出力例
Ingress established at NgrokTunnel: "https://ca**********.ngrok-free.app" -> "http://localhost:8888"
```

これにより、内部の `"http://localhost:8888"` Local URL が、外部からもアクセス可能な `"https://ca**********.ngrok-free.app"` と紐付きました。<br>
この外部アクセス可能な URL (`"https://ca**********.ngrok-free.app"`) は後の設定で使うのでコピーしておいてください。

#### 準備②: Slack App の設定を実施する

次に、Slack 側のアプリの設定を適切に行う必要があります。<br>
[Slack の Your Apps のページ](https://api.slack.com/apps) の「Paper2MD-Translator」App のページから、各種設定を行っていきます。<br>
設定の詳細は、[こちらの pdf ファイル](../paper2md_translator_app_settings.pdf) に載せていますので、併せて確認してください。<br>
「Features」→「Interactivity & Shortcuts」→「Interactivity」の「Request URL」と、「Features」→「Event Subscriptions」→「Enable Events
」の「Request URL」には、先程コピーしておいた ngrok の外部アクセス可能な URL (`"https://ca**********.ngrok-free.app"`) を用いて、`"https://ca**********.ngrok-free.app/slack/events"` を入力してください（`/slack/events`を付けることもお忘れなく）。

これで、運用・管理者側の準備は完了です。<br>
利用者が使えるようになっているはずです。

## Citation

```bibtex
@software{
  author = {Daiki Shiono},
  month = {Aug},
  title = {{Paper2MD-Translator}},
  url = {https://github.com/Onely7/Paper2MD-Translator_for_slack},
  version = {0.1.0},
  year = {2025}
}
```

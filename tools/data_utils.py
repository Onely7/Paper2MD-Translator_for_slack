import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pymupdf
import requests
from PIL import Image


# DATA LOADING
def load_json(filename: str) -> list[Any]:
    """
    Load data from a JSON file.

    Args:
        filename (str): Path to the JSON file.

    Returns:
        List[Any]: Loaded JSON data.

    Example usage:
    >>> data = load_json("data.json")
    """
    with open(filename, encoding="utf-8") as f:
        return json.load(f)


def save_jsonl(data_list: list[dict[str, Any]], filename: str) -> None:
    """
    Save data in JSONL (JSON Lines) format.

    Args:
        filename (str): Path to the output file.
        data_list (List[Dict[str, Any]]): List of data (dict) to be saved.

    Example usage:
    >>> save_jsonl("output.jsonl", [{"name": "example"}])
    """
    filename = str(filename)
    Path(filename).parent.mkdir(parents=True, exist_ok=True)
    with open(filename, "w", encoding="utf-8") as fo:
        for r in data_list:
            fo.write(json.dumps(r, ensure_ascii=False) + "\n")


def extract_pdf_url(text: str) -> str:
    """
    テキスト中から PDF 関連の URL を抽出して返す。
    - arXiv PDF       : https://arxiv.org/pdf/2412.04468 (v2 なども)
    - arXiv abstract  : https://arxiv.org/abs/2412.04468 → PDF URL に書き換え
    - 一般的な .pdf   : https://.../foo.pdf?download=true
    見つからなければ ValueError を送出。
    """
    # 1) arXiv の PDF 形式
    m = re.search(r"(https?://arxiv\.org/pdf/(?P<id>[0-9]+\.[0-9]+(?:v[0-9]+)?))", text)
    if m:
        return m.group(1)

    # 2) arXiv の abstract 形式 → PDF URL に変換
    m = re.search(r"(https?://arxiv\.org/abs/(?P<id>[0-9]+\.[0-9]+(?:v[0-9]+)?))", text)
    if m:
        arxiv_id = m.group("id")
        return f"https://arxiv.org/pdf/{arxiv_id}"

    # 3) その他の一般的な .pdf URL
    m = re.search(r'(https?://[^\s<>"]+\.pdf(?:\?[^ \n\t<>"]*)?)', text)
    if m:
        return m.group(1)

    raise ValueError("テキスト中に有効な PDF URL が見つかりませんでした。")


def download_pdf(url: str, output_dir: str = "./tmp_paper") -> str:
    """
    指定した PDF URL をストリーミングでダウンロードし、
    output_dir に保存したファイルの Path を返す。
    Pathlib を用いてパス操作を行う。
    """
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(url)
    filename = Path(parsed.path).name
    # arXiv/pdf/<id> の場合、拡張子が無いので .pdf を付与
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    file_path = out_dir / filename

    with requests.get(url, stream=True) as r:
        r.raise_for_status()
        with file_path.open("wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

    return str(file_path)


def convert_pdf_to_images(pdf_path, target_size=896):
    """Convert PDF pages to images

    Args:
        pdf_path: Path to PDF file
        target_size: Target size for the longest dimension

    Returns:
        List of PIL Images
    """
    images = []
    try:
        doc = pymupdf.open(pdf_path)

        for page_num in range(len(doc)):
            page = doc[page_num]

            # Calculate scale to make longest dimension equal to target_size
            rect = page.rect
            scale = target_size / max(rect.width, rect.height)

            # Render page as image
            mat = pymupdf.Matrix(scale, scale)
            pix = page.get_pixmap(matrix=mat)

            # Convert to PIL Image
            img_data = pix.tobytes("png")
            pil_image = Image.open(io.BytesIO(img_data))
            images.append(pil_image)

        doc.close()
        print(f"Successfully converted {len(images)} pages from PDF")
        return images

    except Exception as e:
        print(f"Error converting PDF to images: {str(e)}")
        return []


def save_txt(doc: str, filename: str = "output.md"):
    filename = str(filename)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(doc)


def extract_json_string(http_response: str) -> str:
    """
    HTTP レスポンス文字列 http_response から JSON 部分の文字列を返す。
    ヘッダー部とボディ部が空行（\n\n）で区切られていればその後ろを、
    そうでなければ正規表現で {…} を探して抜き出す。
    見つからなければ空文字を返す。
    """
    # 方法1: 空行でスプリット
    parts = http_response.split("\n\n", 1)
    if len(parts) == 2:
        return parts[1].strip()

    # フォールバック: {…} を正規表現で抜き出す
    m = re.search(r"(\{.*\})", http_response, re.DOTALL)
    return m.group(1) if m else ""


def extract_json(http_response: str) -> dict:
    """
    extract_json_string で抜き出した文字列を json.loads にかけて dict に変換する。
    パースに失敗した場合は空 dict を返す。
    """
    json_str = extract_json_string(http_response)
    if not json_str:
        return {}
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return {}

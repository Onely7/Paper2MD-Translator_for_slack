import os
import pathlib
import re
import subprocess

import mdformat
from tqdm import tqdm

from Dolphin.models.api_models.gpt import GPT4o
from Dolphin.utils.tqdm_slack_progress import TqdmSlack
from tools.data_utils import (
    convert_pdf_to_images,
    extract_json,
    load_json,
    save_jsonl,
    save_txt,
)


def merge_bbox(bbox_list):
    # bbox: [x0, y0, x1, y1]
    ## x: 左端からどれだけ離れているか, y: 上端からどれだけ離れているか
    x0_list = []
    y0_list = []
    x1_list = []
    y1_list = []

    for bbox in bbox_list:
        x0, y0, x1, y1 = bbox
        x0_list.append(x0)
        y0_list.append(y0)
        x1_list.append(x1)
        y1_list.append(y1)

    x0 = min(x0_list) - 10
    y0 = min(y0_list) - 10
    x1 = max(x1_list) + 20
    y1 = max(y1_list) + 10

    return [x0, y0, x1, y1]


def create_merge_ok_ids(arr: list[bool]) -> list[int]:
    """
    arr を走査し、
    - 先頭に 0 を置き、
    - 要素が False のときだけカウントを 1 増やし、
    - 要素が True のときはカウントをそのまま
    とした累積カウントリストを返す。
    """
    result = [0]
    for v in arr:
        if v:
            # True のときは増やさない
            result.append(result[-1])
        else:
            # False のときだけ +1
            result.append(result[-1] + 1)
    return result


# 画像とbboxを与えて、その部分を切り取り、画像を保存し、画像の保存先のパスを返す関数
def crop_and_save_image(image, bbox, page, index=0, output_dir="cropped_images"):
    """
    Crop a section of an image based on a bounding box, save the cropped image,
    and return the path to the saved image.

    Args:
        image: PIL Image object.
        bbox: A list or tuple of four integers [x0, y0, x1, y1] representing the bounding box.
        output_dir: Directory to save the cropped images. Defaults to "cropped_images".

    Returns:
        str: The path to the saved cropped image file, or None if an error occurred.
    """
    # Ensure the output directory exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    try:
        # Crop the image
        cropped_image = image.crop(bbox)

        # Generate a unique filename
        filename = f"cropped_image_page{page}_{index}.png"
        output_path = os.path.join(output_dir, filename)

        # Save the cropped image
        cropped_image.save(output_path)

        print(f"Cropped image saved to: {output_path}")
        return output_path

    except Exception as e:
        print(f"Error cropping and saving image: {str(e)}")
        return None


def convert_data_structure(data):
    total_pages = data["total_pages"]
    labels = []
    bboxes = []
    texts = []
    reading_orders = []
    pages = []
    new_reading_orders = []

    new_element_list = []

    cnt = 0
    for page in range(total_pages):
        for i, order in enumerate(data["pages"][page]["elements"]):
            if order["reading_order"] == i:
                labels.append(order["label"])
                bboxes.append(order["bbox"])
                texts.append(order["text"])
                reading_orders.append(order["reading_order"])
                pages.append(page)
                new_reading_orders.append(cnt)

                new_element_list.append(
                    {
                        "label": order["label"],
                        "bbox": order["bbox"],
                        "text": order["text"],
                        "reading_order": order["reading_order"],
                        "page": page,
                        "new_reading_order": cnt,
                    }
                )

                cnt += 1

    return new_element_list


def change_reading_order_based_on_label(element_list):
    element_block_num = -1
    element_block_num_list = []

    for element in element_list:
        if element["label"] in ("title", "author", "sec", "sub_sec", "sub_sub_sec"):
            element_block_num += 1
            element["element_block_num"] = element_block_num
            element_block_num_list.append(element_block_num)
        else:
            element["element_block_num"] = element_block_num

    element_block_list = []
    for element_block_num in element_block_num_list:
        element_block = []
        for element in element_list:
            if element_block_num == element["element_block_num"]:
                element_block.append(element)
        element_block_list.append(element_block)

    most_up_priority_labels = ["title"]
    up_priority_labels = ["tab", "fig", "cap"]
    middle_priority_labels = ["author", "sec", "sub_sec", "sub_sub_sec", "para", "list"]
    down_priority_labels = ["fnote"]
    new_element_block_list = []

    for element_block in element_block_list:
        reading_order_in_block = 0
        new_element_block = []
        for element in element_block:
            if element["label"] in most_up_priority_labels:
                element["reading_order_in_block"] = reading_order_in_block
                new_element_block.append(element)
                reading_order_in_block += 1

        for element in element_block:
            if element["label"] in up_priority_labels:
                element["reading_order_in_block"] = reading_order_in_block
                new_element_block.append(element)
                reading_order_in_block += 1

        for element in element_block:
            if element["label"] in middle_priority_labels:
                element["reading_order_in_block"] = reading_order_in_block
                new_element_block.append(element)
                reading_order_in_block += 1

        for element in element_block:
            if element["label"] in down_priority_labels:
                element["reading_order_in_block"] = reading_order_in_block
                new_element_block.append(element)
                reading_order_in_block += 1

        new_element_block_list.append(new_element_block)

    return new_element_block_list


def attach_merge_id(element_block_list):
    middle_priority_labels = ["author", "sec", "sub_sec", "sub_sub_sec", "para", "list"]
    updated_element_block_list = []
    for element_block in element_block_list:
        new_element_block = []
        merge_id = -1
        first_merge_in_block = False
        for i, element in enumerate(element_block):
            if element["label"] not in middle_priority_labels:
                merge_id += 1
                element["merge_id"] = merge_id
                new_element_block.append(element)
                first_merge_in_block = True
            elif element["label"] in middle_priority_labels:
                if i == 0:
                    element["merge_id"] = merge_id
                    _b_x0, _b_y0, _b_x1, b_y1 = element["bbox"]
                    new_element_block.append(element)
                elif first_merge_in_block:
                    merge_id += 1
                    element["merge_id"] = merge_id
                    _b_x0, _b_y0, _b_x1, b_y1 = element["bbox"]
                    new_element_block.append(element)
                    first_merge_in_block = False
                else:
                    _x0, y0, _x1, _y1 = element["bbox"]
                    # 同じlabel(e.g.,"para")だが、pageやcolumnを跨ぐ場合は別のmerge_idを付与
                    if abs(b_y1 - y0) < 100:
                        element["merge_id"] = merge_id
                        _b_x0, _b_y0, _b_x1, b_y1 = element["bbox"]
                        new_element_block.append(element)
                    else:
                        merge_id += 1
                        element["merge_id"] = merge_id
                        _b_x0, _b_y0, _b_x1, b_y1 = element["bbox"]
                        new_element_block.append(element)

        updated_element_block_list.append(new_element_block)

    return updated_element_block_list


def reblock_based_on_merge_id(element_block_list):
    new_element_block_list = []
    for element_block in element_block_list:
        element_merge_block_num_set = set()
        for element in element_block:
            element_merge_block_num_set.add(element["merge_id"])
        element_merge_block_num_list = sorted(list(element_merge_block_num_set))

        for element_merge_block_num in element_merge_block_num_list:
            element_merge_block = []
            for element in element_block:
                if element["merge_id"] == element_merge_block_num:
                    element_merge_block.append(element)
            new_element_block_list.append(element_merge_block)

    return new_element_block_list


def merge_elements_in_block(element_block_list):
    new_element_list = []
    for i, element_block in enumerate(element_block_list):
        merged_bbox = []
        merged_text = ""
        for j, element in enumerate(element_block):
            if len(element_block) == 1:
                first_label = element["label"]
                last_label = element["label"]
            elif j == 0:
                first_label = element["label"]
            elif j == len(element_block) - 1:
                last_label = element["label"]
            merged_bbox.append(element["bbox"])
            merged_text += element["text"]
            page = element["page"]
            if element["label"] in ("fig", "tab", "cap"):
                label = element["label"]
            else:
                label = "merged_label"
        new_element = {
            "label": label,
            "first_label": first_label,
            "last_label": last_label,
            "bbox": merge_bbox(merged_bbox),
            "text": merged_text,
            "reading_order": i,
            "page": page,
        }

        new_element_list.append(new_element)

    return new_element_list


def merge_similar_elements(element_list):
    new_element_pairs = [element_list[i : i + 2] for i in range(len(element_list) - 1)]
    is_merge_ok_list = []
    for pair in new_element_pairs:
        merge_ok_labels = ["para", "list"]
        if (pair[0]["last_label"] in (merge_ok_labels)) and (
            pair[1]["first_label"] in (merge_ok_labels)
        ):
            is_merge_ok_list.append(True)
        else:
            is_merge_ok_list.append(False)

    merge_ok_ids = create_merge_ok_ids(is_merge_ok_list)

    updated_element_list = []
    tmp_merge_element_list = []
    merge_ok_count = 0
    for i, merge_ok_id in enumerate(merge_ok_ids):
        if merge_ok_count == merge_ok_id:
            tmp_merge_element_list.append(element_list[i])
        else:
            updated_element_list.append(tmp_merge_element_list)
            tmp_merge_element_list = [element_list[i]]
            merge_ok_count += 1

    updated_element_list.append(tmp_merge_element_list)

    element_pairs = [
        updated_element_list[i : i + 2] for i in range(len(updated_element_list) - 1)
    ]
    for i in range(len(element_pairs)):
        pair_0 = element_pairs[i][0][0]
        pair_1 = element_pairs[i][1][0]
        if (pair_0["last_label"] in ("tab", "fig")) and (
            pair_1["first_label"] in ("cap")
        ):
            if (
                updated_element_list[i][0].get("bbox")
                and updated_element_list[i][0].get("last_label")
                and updated_element_list[i + 1][0].get("first_label") == "cap"
                and updated_element_list[i + 1][0].get("bbox")
            ):
                updated_element_list[i][0]["bbox"] = merge_bbox(
                    [
                        updated_element_list[i][0]["bbox"],
                        updated_element_list[i + 1][0]["bbox"],
                    ]
                )

    return updated_element_list


def integrate_data_fields_in_element(element_list):
    new_elements = []
    reading_order = 0
    for elements in element_list:
        bboxes = []
        pages = []
        merged_text = ""
        for i, element in enumerate(elements):
            if i == 0:
                first_label = element["first_label"]
            bboxes.append(element["bbox"])
            pages.append(element["page"])
            merged_text += element["text"]
            if element["label"] in ("fig", "tab", "cap"):
                label = element["label"]
            else:
                label = "merged_label"
        new_element = {
            "label": label,
            "first_label": first_label,
            "bboxes": bboxes,
            "pages": pages,
            "text": merged_text,
            "reading_order": reading_order,
        }
        reading_order += 1
        new_elements.append(new_element)

    return new_elements


def crop_and_save_image_in_all_elements(element_list, images, output_dir):
    crop_id = 0
    for element in element_list:
        image_paths = []
        for bbox, page in zip(element["bboxes"], element["pages"]):
            image_path = crop_and_save_image(
                images[page], bbox, page + 1, index=crop_id, output_dir=output_dir
            )
            image_paths.append(image_path)
            crop_id += 1
        element["images"] = image_paths


def load_api_models(model_name: str, image_dir: str, api_key: str):
    model = GPT4o(pretrained=model_name, image_dir=image_dir, api_key=api_key)

    return model


def prepare_llm_input_format(data) -> dict:
    """(Example)
    data
    {
        'label': 'merged_label',
        'bboxes': [
            [122, 1334, 630, 1625],
            [639, 918, 1147, 1625],
            [122, 1224, 630, 1625],
            [639, 670, 1147, 1371]
        ],
        'pages': [4, 4, 5, 5],
        'text': 'New ... results.',
        'reading_order': 34,
        'images': [
            'cropped_images/cropped_image_page5_41.png',
            'cropped_images/cropped_image_page5_42.png',
            'cropped_images/cropped_image_page6_43.png',
            'cropped_images/cropped_image_page6_44.png'
        ]
    }
    """
    text = (
        "あなたは最高の日本のAI研究者です。\n"
        "与えられた論文のセクション画像もしくは文章を自然な日本語に翻訳してください。\n\n"
        "ただし、以下に示すルールはすべて必ず遵守してください。\n"
        "- 必ず省略及び意訳はせずに、与えられた全ての文書を忠実に翻訳すること。\n"
        "- 必ず翻訳結果だけを markdown 形式で出力するようにすること。\n"
        "- セクション画像が与えられた場合、絶対に画像中に書かれている翻訳元の英語テキストを出力せずに、**直接翻訳結果だけを出力** すること。\n"
        "- 太字や斜体で書かれている **文** は不自然にならない限り、**翻訳結果だけ** を出力すること（ただし、太字や斜体で書かれている単語や名詞句はこの限りではなく、翻訳元の英語テキストだけを出力することが望ましい）。\n"
        "- markdown であることを示す codeblock は含めないこと。\n"
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
        "  - 不適切な例(✗): '$$\n||max\_{\\mathbf{z}} p(\\mathbf{h}|\\mathbf{z}) p(\\mathbf{z})\n$$'\n"
        "  - 適切な例(◯): '$$\n\max_{\mathbf{z}} \, p(\mathbf{h}\mid\mathbf{z})\,p(\mathbf{z})\n$$'\n"
        "- 引用番号（例: Qwen-2 [14]）がある場合は、省略せずにその引用番号も出力すること。\n"
        '- footnote に誘導する記号（例: [...] using sentencepiece*.）がある場合は、省略せずにその誘導記号（例の場合 "*"）も出力すること。ここでも見出し表現（# シンボル）を使ってはいけません。\n\n'
        "再度の警告ですが、必ず省略及び意訳はせずに、与えられた全ての文書を忠実に (codeblock は含めない) markdown 形式で (翻訳元の英語テキストを含まない) **自然な日本語** に翻訳してください。\n"
        "また、翻訳が難しい場合は、「申し訳ありません。入力ソースから上手く翻訳ができませんでした。」と出力してください。"
    )

    # Prepare the input dictionary for the LLM
    input_dict = {
        "text": text,
        "images": data["images"],  # Load the corresponding image for context
        "system_prompt": None,  # Task-specific instructions
        "few_shot_data": None,  # Few-shot examples for better generation
        "gen_kwargs": None,  # Generation parameters
    }

    return input_dict


def llm_generate_text_from_image(model, data):
    """(Example)
    data
    {
        'label': 'merged_label',
        'bboxes': [
            [122, 1334, 630, 1625],
            [639, 918, 1147, 1625],
            [122, 1224, 630, 1625],
            [639, 670, 1147, 1371]
        ],
        'pages': [4, 4, 5, 5],
        'text': 'New ... results.',
        'reading_order': 34,
        'images': [
            'cropped_images/cropped_image_page5_41.png',
            'cropped_images/cropped_image_page5_42.png',
            'cropped_images/cropped_image_page6_43.png',
            'cropped_images/cropped_image_page6_44.png'
        ]
    }
    """
    text = (
        "あなたは最高の日本のAI研究者です。\n"
        "与えられた論文のセクション画像もしくは文章を自然な日本語に翻訳してください。\n\n"
        "ただし、以下に示すルールはすべて必ず遵守してください。\n"
        "- 必ず省略及び意訳はせずに、与えられた全ての文書を忠実に翻訳すること。\n"
        "- 必ず翻訳結果だけを markdown 形式で出力するようにすること。\n"
        "- セクション画像が与えられた場合、絶対に画像中に書かれている翻訳元の英語テキストを出力せずに、**直接翻訳結果だけを出力** すること。\n"
        "- 太字や斜体で書かれている **文** は不自然にならない限り、**翻訳結果だけ** を出力すること（ただし、太字や斜体で書かれている単語や名詞句はこの限りではなく、翻訳元の英語テキストだけを出力することが望ましい）。\n"
        "- markdown であることを示す codeblock は含めないこと。\n"
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
        "- 数式をブロックとして追加する場合は、式を 2 つのドル記号 ($$) で区切ること。\n"
        "- 行中に明らかにコードのような表現要素がある場合は、インラインコードを使用して要素をバッククォート (`) で囲むこと。\n"
        "- 引用番号（例: Qwen-2 [14]）がある場合は、省略せずにその引用番号も出力すること。\n"
        '- footnote に誘導する記号（例: [...] using sentencepiece*.）がある場合は、省略せずにその誘導記号（例の場合 "*"）も出力すること。ここでも見出し表現（# シンボル）を使ってはいけません。\n\n'
        "再度の警告ですが、必ず省略及び意訳はせずに、与えられた全ての文書を忠実に (codeblock は含めない) markdown 形式で (翻訳元の英語テキストを含まない) **自然な日本語** に翻訳してください。\n"
        "また、翻訳が難しい場合は、「申し訳ありません。入力ソースから上手く翻訳ができませんでした。」と出力してください。"
    )

    # Generate the response using the model
    response = model.generate_response(
        text=text,
        images=data["images"],  # Load the corresponding image for context
        system_prompt=None,  # Task-specific instructions
        few_shot_data=None,  # Few-shot examples for better generation
        gen_kwargs=None,  # Generation parameters
    )

    return response


def llm_rewrite_title_text(model, text):
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
        f"Before revision (input text): {text}\n"
        "After revision (output text): "
    )

    # Generate the response using the model
    response = model.generate_response(
        text=prompt,
        images=None,  # Load the corresponding image for context
        system_prompt=None,  # Task-specific instructions
        few_shot_data=None,  # Few-shot examples for better generation
        gen_kwargs=None,  # Generation parameters
    )

    return response


def llm_rewrite_reference_text(model, text):
    prompt = (
        "Please refine the given reference entries into a natural format.\n"
        "Be sure not to omit or paraphrase anything; output all of the provided text faithfully in Markdown format (do not enclose it in code blocks).\n"
        'When generating output texts, always start with the text "## Reference\\n".\n'
        "Below is an example of the ideal output after rewriting.\n"
        "However, when generating the output text, make absolutely sure not to reproduce this example.\n"
        "## Reference\n"
        "- [1] Haotian Liu, Chunyuan Li, Qingyang Wu, and Yong Jae Lee. Visual Instruction Tuning. In Con- ference on Neural Information Processing Systems (NeurIPS), 2024.\n"
        "- [2] Ji Lin, Hongxu Yin, Wei Ping, Yao Lu, Pavlo Molchanov, Andrew Tao, Huizi Mao, Jan Kautz, Mohammad Shoeybi, and Song Han. VILA: On Pre- training for Visual Language Models. In IEEE/CVF Conference on Computer Vision and Pattern Recog- nition (CVPR), 2024.\n"
        "- [3] Haotian Liu, ...\n"
        f"{'=' * 50}\n"
        "\n"
        f"{text}\n"
        "\n"
        f"{'-' * 50}\n"
        "\n"
    )

    # Generate the response using the model
    response = model.generate_response(
        text=prompt,
        images=None,  # Load the corresponding image for context
        system_prompt=None,  # Task-specific instructions
        few_shot_data=None,  # Few-shot examples for better generation
        gen_kwargs=None,  # Generation parameters
    )

    return response


def llm_summary_document(model, text):
    prompt = (
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

    # Generate the response using the model
    response = model.generate_response(
        text=prompt + text,
        images=None,  # Load the corresponding image for context
        system_prompt=None,  # Task-specific instructions
        few_shot_data=None,  # Few-shot examples for better generation
        gen_kwargs=None,  # Generation parameters
    )

    return response


def transcribing_text_from_images_using_lvlm_with_slack_tqdm(
    element_list,
    model,
    image_dir,
    use_gyazo,
    client,  # ★ WebClient を受け取る
    channel,  # ★ 投稿先チャンネル ID
    thread_ts,  # ★ スレッド TS
    api_keys,  # ★ API キーを含む辞書
):
    fig_tab_images_paths = []

    with TqdmSlack(
        total=len(element_list),
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        desc="画像 → テキスト 変換中",
    ) as pbar:
        for element in element_list:
            # title には LLM 生成テキストを適用しない
            if element["first_label"] in ("title"):
                element["llm_generated_text_from_img"] = None
                # element["md"] = f"# {element['text']}"
                element["md"] = llm_rewrite_title_text(model, f"# {element['text']}")
            elif element["label"] in ("merged_label", "cap"):
                # Reference の場合は、llm_rewrite_reference_text 関数を使用
                if element["text"].strip().startswith("Reference"):
                    element["md"] = llm_rewrite_reference_text(model, element["text"])
                else:
                    llm_generate_text = llm_generate_text_from_image(model, element)
                    # うまく翻訳できなかった場合は、改行文字だけを使用
                    if "申し訳" in llm_generate_text:
                        element["llm_generated_text_from_img"] = "\n"
                    elif element["first_label"] in ("author", "sec"):
                        element["llm_generated_text_from_img"] = (
                            "\n\n\n" + llm_generate_text
                        )
                    elif element["first_label"] in ("sub_sec", "sub_sub_sec"):
                        element["llm_generated_text_from_img"] = (
                            "\n\n" + llm_generate_text
                        )
                    else:
                        element["llm_generated_text_from_img"] = (
                            "\n" + llm_generate_text_from_image(model, element)
                        )
                    element["md"] = element["llm_generated_text_from_img"]
            else:
                # title, merged_label, cap 以外の first_label の付いた element (fig, tab) に対しては画像翻訳を実施せず、
                # 画像の html タグを生成する
                element["llm_generated_text_from_img"] = None
                fig_tab_images_paths.append(
                    str(pathlib.Path(image_dir).joinpath(element["images"][0]))
                )
                # use_gyazo が True の場合は Gyazo にアップロードして画像 URL を取得し、これを img タグに埋め込む
                if use_gyazo:
                    # Read the registered API key
                    subprocess.call("ls")
                    command = [
                        "curl",
                        "-i",
                        f"https://upload.gyazo.com/api/upload?access_token={api_keys['gyazo']}",
                        "-F",
                        f"imagedata=@{str(pathlib.Path(image_dir).joinpath(element['images'][0]))}",
                    ]
                    res = subprocess.run(command, capture_output=True, encoding="utf-8")
                    image_url = extract_json(res.stdout)["url"]
                    element["md"] = (
                        f'\n\n<img src="{image_url}" width=60% alt="{str(pathlib.Path(image_dir).joinpath(element["images"][0]))}">\n'
                    )
                # use_gyazo が False の場合は、ローカルの画像パスを img タグに埋め込む
                else:
                    element["md"] = (
                        f"\n\n<img src={str(pathlib.Path(image_dir).joinpath(element['images'][0]))} width=60%>\n"
                    )
            pbar.update(1)

        return element_list, fig_tab_images_paths


def transcribing_text_from_images_using_lvlm_with_slack_tqdm_batch(
    element_list,
    model,
    image_dir,
    use_gyazo,
    client,  # ★ WebClient を受け取る
    channel,  # ★ 投稿先チャンネル ID
    thread_ts,  # ★ スレッド TS
    api_keys,  # ★ API キーを含む辞書
):
    fig_tab_images_paths = []
    input_batch = [prepare_llm_input_format(element) for element in element_list]

    with TqdmSlack(
        total=1,
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        desc="画像 → テキスト 変換中",
    ) as pbar:
        # Generate the response using the model
        responses = model.generate_batch_response(input_batch)
        pbar.update(1)

    for element, response in zip(element_list, responses):
        element["llm_generated_text_from_img"] = response

    for element in element_list:
        # title には LLM 生成テキストを適用しない
        if element["first_label"] in ("title"):
            element["llm_generated_text_from_img"] = None
            element["md"] = llm_rewrite_title_text(model, f"# {element['text']}")
        elif element["label"] in ("merged_label", "cap"):
            # Reference の場合は、llm_rewrite_reference_text 関数を使用
            if element["text"].strip().startswith("Reference"):
                element["md"] = llm_rewrite_reference_text(model, element["text"])
            else:
                llm_generate_text = element["llm_generated_text_from_img"]
                # うまく翻訳できなかった場合は、改行文字だけを使用
                if "申し訳" in llm_generate_text:
                    element["llm_generated_text_from_img"] = "\n"
                elif element["first_label"] in ("author", "sec"):
                    element["llm_generated_text_from_img"] = (
                        "\n\n\n" + llm_generate_text
                    )
                elif element["first_label"] in ("sub_sec", "sub_sub_sec"):
                    element["llm_generated_text_from_img"] = "\n\n" + llm_generate_text
                else:
                    element["llm_generated_text_from_img"] = (
                        "\n" + element["llm_generated_text_from_img"]
                    )
                element["md"] = element["llm_generated_text_from_img"]
        else:
            # title, merged_label, cap 以外の first_label の付いた element (fig, tab) に対しては画像翻訳を実施せず、
            # 画像の html タグを生成する
            element["llm_generated_text_from_img"] = None
            fig_tab_images_paths.append(
                str(pathlib.Path(image_dir).joinpath(element["images"][0]))
            )
            # use_gyazo が True の場合は Gyazo にアップロードして画像 URL を取得し、これを img タグに埋め込む
            if use_gyazo:
                # Read the registered API key
                subprocess.call("ls")
                command = [
                    "curl",
                    "-i",
                    f"https://upload.gyazo.com/api/upload?access_token={api_keys['gyazo']}",
                    "-F",
                    f"imagedata=@{str(pathlib.Path(image_dir).joinpath(element['images'][0]))}",
                ]
                res = subprocess.run(command, capture_output=True, encoding="utf-8")
                image_url = extract_json(res.stdout)["url"]
                element["md"] = (
                    f'\n\n<img src="{image_url}" width=60% alt="{str(pathlib.Path(image_dir).joinpath(element["images"][0]))}">\n'
                )
            # use_gyazo が False の場合は、ローカルの画像パスを img タグに埋め込む
            else:
                element["md"] = (
                    f"\n\n<img src={str(pathlib.Path(image_dir).joinpath(element['images'][0]))} width=60%>\n"
                )

    return element_list, fig_tab_images_paths


def transcribing_text_from_images_using_lvlm(
    element_list, model, image_dir, use_gyazo, api_keys
):
    fig_tab_images_paths = []
    for element in tqdm(element_list):
        # title には LLM 生成テキストを適用しない
        if element["first_label"] in ("title"):
            element["llm_generated_text_from_img"] = None
            element["md"] = llm_rewrite_title_text(model, f"# {element['text']}")
        elif element["label"] in ("merged_label", "cap"):
            # Reference の場合は、llm_rewrite_text 関数を使用
            if element["text"].strip().startswith("Reference"):
                element["md"] = llm_rewrite_reference_text(model, element["text"])
            else:
                llm_generate_text = llm_generate_text_from_image(model, element)
                # うまく翻訳できなかった場合は、改行文字だけを使用
                if "申し訳" in llm_generate_text:
                    element["llm_generated_text_from_img"] = "\n"
                elif element["first_label"] in ("author", "sec"):
                    element["llm_generated_text_from_img"] = (
                        "\n\n\n" + llm_generate_text
                    )
                elif element["first_label"] in ("sub_sec", "sub_sub_sec"):
                    element["llm_generated_text_from_img"] = "\n\n" + llm_generate_text
                else:
                    element["llm_generated_text_from_img"] = (
                        "\n" + llm_generate_text_from_image(model, element)
                    )
                element["md"] = element["llm_generated_text_from_img"]
        else:
            # title, merged_label, cap 以外の first_label の付いた element (fig, tab) に対しては画像翻訳を実施せず、
            # 画像の html タグを生成する
            element["llm_generated_text_from_img"] = None
            fig_tab_images_paths.append(
                str(pathlib.Path(image_dir).joinpath(element["images"][0]))
            )
            # use_gyazo が True の場合は Gyazo にアップロードして画像 URL を取得し、これを img タグに埋め込む
            if use_gyazo:
                # Read the registered API key
                subprocess.call("ls")
                command = [
                    "curl",
                    "-i",
                    f"https://upload.gyazo.com/api/upload?access_token={api_keys['gyazo']}",
                    "-F",
                    f"imagedata=@{str(pathlib.Path(image_dir).joinpath(element['images'][0]))}",
                ]
                res = subprocess.run(command, capture_output=True, encoding="utf-8")
                image_url = extract_json(res.stdout)["url"]
                element["md"] = (
                    f'\n\n<img src="{image_url}" width=60% alt="{str(pathlib.Path(image_dir).joinpath(element["images"][0]))}">\n'
                )
            # use_gyazo が False の場合は、ローカルの画像パスを img タグに埋め込む
            else:
                element["md"] = (
                    f"\n\n<img src={str(pathlib.Path(image_dir).joinpath(element['images'][0]))} width=60%>\n"
                )

    return element_list, fig_tab_images_paths


def get_markdown_from_element_list(element_list):
    """
    Convert a list of elements into a single Markdown string.
    Each element's 'md' field is concatenated with a newline.
    """
    markdown_all = ""
    for element in element_list:
        markdown_all += element["md"] + "\n"
    return markdown_all.strip()  # Remove trailing newline


def heading_level_from_prefix(prefix: str) -> int | None:
    """
    ドット区切りセグメント数で見出しレベルを決定。
    ただし先頭セグメントは以下のときのみ有効:
      * 完全に数字             (例: 4, 12)
      * 長さ 3 以下の英字列     (例: A, B, II, III)
    それ以外（Figure, Table など）は None を返して無視。
    """
    segments = re.findall(r"[A-Za-z0-9]+", prefix)
    depth = len(segments)
    if not (1 <= depth <= 3):
        return None

    first = segments[0]
    if not (first.isdigit() or len(first) <= 3):
        return None

    return {1: 2, 2: 3, 3: 4}[depth]


def transform_markdown(md: str) -> str:
    """
    4 見出しルール +
    * / _ / ** / __ の強調除去 +
    改行分離を行わず、本文があれば同じ行に結合
    """
    out: list[str] = []

    # ===== ① キーワード行（強調あり／なし） ==============================
    pat_kw = re.compile(
        r"""^\s*
            (?:\*{1,2}|_{1,2})?          # 開始強調 (*, **, _, __) 任意
            \s*
            (?P<kw>abstract|reference|appendix|acknowledgments)
            \s*
            (?:\*{1,2}|_{1,2})?          # 終了強調 任意
            \s*
            (?P<rest>.*)$
        """,
        re.I | re.VERBOSE,
    )

    # ===== ② 強調付き番号見出し (* or _ or ** or __) =====================
    pat_em_num = re.compile(
        r"""^\s*
            (?P<em>\*{1,2}|_{1,2})       # 開始強調
            \s*
            (?P<prefix>[A-Za-z0-9]+(?:\.[A-Za-z0-9]+){0,2})  # 1〜3 セグメント
            (?:\.\s+|\s+)                # ← 末尾ピリオドは任意
            (?P<title>.*?)               # 見出しタイトル
            \s*
            (?P=em)                      # 同じ強調で閉じる
            \s*
            (?P<rest>.*)$                # その行の残り（本文）
        """,
        re.VERBOSE,
    )

    # ===== ③ 通常の番号見出し ============================================
    pat_num = re.compile(
        r"""^\s*
            (?P<prefix>[A-Za-z0-9]+(?:\.[A-Za-z0-9]+){0,2})  # 1〜3 セグメント
            (?:\.\s+|\s+)                # ← 末尾ピリオドは任意
            (?P<title>.*?)               # 見出しタイトル
            (?P<rest>\s+.*)?$            # 同じ行の残り（任意）
        """,
        re.VERBOSE,
    )

    for raw in md.splitlines():
        # 行頭の # を一旦取り除き、内容を解析
        body = re.sub(r"^\s*#+\s*", "", raw)

        # ---------- ① キーワード行 ----------
        if m := pat_kw.match(body):
            kw = m["kw"].capitalize()
            rest = m["rest"].strip()
            line = f"## {kw}" + (f" {rest}" if rest else "")
            out.append(line)
            continue

        # ---------- ② 強調付き番号見出し ----------
        if m := pat_em_num.match(body):
            if (level := heading_level_from_prefix(m["prefix"])) is not None:
                rest = m["rest"].strip()
                line = f"{'#' * level} {m['prefix']}. {m['title'].strip()}"
                if rest:
                    line += f" {rest}"
                out.append(line)
                continue

        # ---------- ③ 通常の番号見出し ----------
        if m := pat_num.match(body):
            if (level := heading_level_from_prefix(m["prefix"])) is not None:
                rest = (m["rest"] or "").strip()
                line = f"{'#' * level} {m['prefix']}. {m['title'].strip()}"
                if rest:
                    line += f" {rest}"
                out.append(line)
                continue

        # ---------- ④ それ以外 ----------
        out.append(raw)

    return "\n".join(out)


def rewrite_special_symbols(md: str) -> str:
    md = re.sub(r"\\([<>_])", r"\1", md)

    # ③ ダブルバックスラッシュ（\\）が本当に残っているケース
    #    （raw 文字列を読み込んだ場合など）にも備えるなら最後にもう一度
    md = md.replace(r"\\", r"\\"[::-1])  # → "\" 1 個に

    return md


def create_natural_md_with_llm(
    paper_path,
    save_dir,
    model_name="gpt-4.1-mini",
    use_gyazo=True,
    target_size=1792,
    do_batch=False,
    client=None,
    channel=None,
    thread_ts=None,
    api_keys=None,
):
    if use_gyazo and not api_keys.get("gyazo", None):
        raise ValueError(
            "use_gyazo is True, but GYAZO_ACCESS_TOKEN is not set in api_keys."
        )
    paper_path = pathlib.Path(paper_path)
    save_dir = pathlib.Path(save_dir)
    # mkdir
    image_save_dir = save_dir.joinpath(f"cropped_images/{paper_path.stem}")
    elements_save_file_path = save_dir.joinpath(
        f"elements_with_lvlm/{paper_path.stem}.jsonl"
    )
    md_save_file_path = save_dir.joinpath(f"markdown_with_lvlm/{paper_path.stem}.md")
    save_dir.mkdir(parents=True, exist_ok=True)
    image_save_dir.mkdir(parents=True, exist_ok=True)
    elements_save_file_path.parent.mkdir(parents=True, exist_ok=True)
    md_save_file_path.parent.mkdir(parents=True, exist_ok=True)
    # get images from pdf
    images = convert_pdf_to_images(paper_path, target_size=target_size)

    model = load_api_models(
        model_name, image_dir=image_save_dir, api_key=api_keys["openai"]
    )
    model_for_summary = load_api_models(
        "o3", image_dir=image_save_dir, api_key=api_keys["openai"]
    )
    print(str(save_dir.joinpath(f"recognition_json/{paper_path.stem}.json")))
    data = load_json(save_dir.joinpath(f"recognition_json/{paper_path.stem}.json"))

    element_list = convert_data_structure(data)
    element_block_list = change_reading_order_based_on_label(element_list)
    element_block_list = attach_merge_id(element_block_list)
    element_block_list = reblock_based_on_merge_id(element_block_list)
    element_list = merge_elements_in_block(element_block_list)
    element_list = merge_similar_elements(element_list)
    element_list = integrate_data_fields_in_element(element_list)
    crop_and_save_image_in_all_elements(element_list, images, image_save_dir)
    if do_batch and client and channel and thread_ts:
        # Use the Slack tqdm wrapper if client, channel, and thread_ts are provided
        element_list, fig_tab_images_paths = (
            transcribing_text_from_images_using_lvlm_with_slack_tqdm_batch(
                element_list,
                model,
                image_save_dir,
                use_gyazo,
                client,
                channel,
                thread_ts,
                api_keys,
            )
        )
    elif client and channel and thread_ts:
        # Use the Slack tqdm wrapper if client, channel, and thread_ts are provided
        element_list, fig_tab_images_paths = (
            transcribing_text_from_images_using_lvlm_with_slack_tqdm(
                element_list,
                model,
                image_save_dir,
                use_gyazo,
                client,
                channel,
                thread_ts,
                api_keys,
            )
        )
    else:
        # Use the regular tqdm if Slack parameters are not provided
        element_list, fig_tab_images_paths = transcribing_text_from_images_using_lvlm(
            element_list,
            model,
            image_save_dir,
            use_gyazo,
            api_keys,
        )
    save_jsonl(element_list, filename=elements_save_file_path)
    markdown_all = get_markdown_from_element_list(element_list)
    markdown_all = mdformat.text(markdown_all)
    markdown_all = transform_markdown(markdown_all)
    markdown_all = rewrite_special_symbols(markdown_all)
    markdown_all = mdformat.text(markdown_all)
    save_txt(markdown_all, filename=md_save_file_path)
    summary = llm_summary_document(model_for_summary, markdown_all)

    return summary, md_save_file_path, fig_tab_images_paths

from create_md import create_natural_md_with_llm
from Dolphin.demo_page_hf import dolphin
from Dolphin.demo_page_hf_with_slack_tqdm import dolphin_with_slack_tqdm


def url2md_translator(
    paper_path,
    save_dir="/work00/paper2md_translator/results",
    use_gyazo=True,
    max_image_resolution=1792,
    do_batch=False,
    client=None,
    channel=None,
    thread_ts=None,
    api_keys=None,
):
    if client and channel and thread_ts:
        # Dolphin (Document Image Parsing via Heterogeneous Anchor Prompting)
        dolphin_with_slack_tqdm(
            model_path="./Dolphin/hf_model",
            input_path=paper_path,
            save_dir=save_dir,
            max_batch_size=32,
            target_size=max_image_resolution,
            client=client,
            channel=channel,
            thread_ts=thread_ts,
        )
    else:
        # Dolphin (Document Image Parsing via Heterogeneous Anchor Prompting)
        dolphin(
            model_path="./Dolphin/hf_model",
            input_path=paper_path,
            save_dir=save_dir,
            max_batch_size=32,
            target_size=max_image_resolution,
        )

    # Convert elements (title, abstract, section, sub-section, figure, table, ...) contained in the paper pdf in natural order
    # , retrieve the translation results from the text images in the elements using LLM,
    # and save the markdown files, image files and table files.
    summary, md_path, fig_tab_image_paths = create_natural_md_with_llm(
        paper_path,
        save_dir,
        model_name="gpt-4.1-mini",
        use_gyazo=use_gyazo,
        target_size=max_image_resolution,
        do_batch=do_batch,
        client=client,
        channel=channel,
        thread_ts=thread_ts,
        api_keys=api_keys,
    )

    return summary, md_path, fig_tab_image_paths


if __name__ == "__main__":
    paper_path = "/work00/paper2md_translator/resources/llava_paper_example.pdf"
    save_dir = "/work00/paper2md_translator/results"
    use_gyazo = True

    summary, md_path, fig_tab_image_paths = url2md_translator(
        paper_path,
        save_dir=save_dir,
        use_gyazo=True,
        max_image_resolution=3584,
    )

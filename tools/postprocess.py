def formatting_summary_section(summary: str) -> str:
    # pattern 1
    target_sections = (
        "*【背景】*",
        "*【目的】*",
        "*【手法】*",
        "*【実験方法】*",
        "*【実験結果】*",
        "*【考察】*",
    )
    ok_cnt = 0
    for sec in target_sections:
        if sec in summary:
            ok_cnt += 1
    if ok_cnt == len(target_sections):
        return summary

    # pattern 2
    target_sections = (
        "【背景】",
        "【目的】",
        "【手法】",
        "【実験方法】",
        "【実験結果】",
        "【考察】",
    )
    for sec in target_sections:
        if sec in summary:
            summary = summary.replace(sec, f"*{sec}*")

    return summary

#!/usr/bin/env python3
import os
import re
from typing import List, Tuple

import pandas as pd
import openpyxl  # noqa: F401


INPUT_PATH = "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/2025-8-4病理和B超报告withjpg.xlsx"
OUTPUT_PATH = "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/2025-8-4病理和B超报告withjpg_labeled.xlsx"
DIAG_COL = "DIAGNOSE_DESC"


def _compile_patterns(patterns: List[Tuple[str, str]]) -> List[Tuple[str, re.Pattern]]:
    return [(name, re.compile(pattern, re.IGNORECASE)) for name, pattern in patterns]


NEGATED_POSITIVE_PATTERNS = _compile_patterns(
    [
        ("未见/无 + 癌恶性", r"(?:未见|未查见|未发现|无|没有|否认)[^。；;，,\n]{0,12}(?:恶性肿瘤|恶性|癌|carcinoma|adenocarcinoma|carcinosarcoma|cancer)"),
        ("排除/除外 + 癌恶性", r"(?:排除|除外|不支持)[^。；;，,\n]{0,12}(?:恶性肿瘤|恶性|癌|carcinoma|adenocarcinoma|carcinosarcoma|cancer)"),
        ("未见明显恶性", r"未见明显恶性"),
        ("无恶性", r"无恶性"),
        ("未见癌", r"未见癌"),
        ("未见恶性", r"未见恶性"),
        ("no carcinoma/malignancy", r"\b(?:no|without)\b[^.;,\n]{0,15}(?:malignan\w*|carcinoma|adenocarcinoma|cancer)"),
    ]
)

NEGATED_PRECANCER_PATTERNS = _compile_patterns(
    [
        (
            "未见/无 + 癌前关键词",
            r"(?:未见|未发现|无|排除|除外|不支持)[^。；;，,\n]{0,15}(?:非典型增生|不典型增生|EIN|癌前病变|高级别病变|endometrial\s+intraepithelial\s+neoplasia|atypical\s+endometrial\s+hyperplasia)",
        )
    ]
)

POSITIVE_DIRECT_PATTERNS = _compile_patterns(
    [
        ("子宫内膜样腺癌", r"子宫内膜样腺癌"),
        ("子宫内膜样癌", r"子宫内膜样癌"),
        ("子宫内膜癌", r"子宫内膜癌"),
        ("内膜癌", r"内膜癌"),
        ("endometrioid carcinoma", r"endometrioid\s+carcinoma"),
    ]
)

POSITIVE_CONTEXT_PATTERNS = _compile_patterns(
    [
        ("宫内物", r"宫内物"),
        ("宫内容物", r"宫内容物"),
        ("宫腔", r"宫腔"),
        ("子宫内膜", r"子宫内膜"),
        ("诊刮", r"诊刮"),
        ("刮宫", r"刮宫"),
        ("endometrial", r"endometrial"),
        ("endometrium", r"endometrium"),
    ]
)

POSITIVE_MALIGNANT_PATTERNS = _compile_patterns(
    [
        ("高级别浆液性癌", r"高级别浆液性癌"),
        ("浆液性癌", r"浆液性癌"),
        ("透明细胞癌", r"透明细胞癌"),
        ("癌肉瘤", r"癌肉瘤"),
        ("腺癌", r"腺癌"),
        ("浸润癌", r"浸润癌"),
        ("低分化癌", r"低分化癌"),
        ("恶性肿瘤", r"恶性肿瘤"),
        ("serous carcinoma", r"serous\s+carcinoma"),
        ("clear cell carcinoma", r"clear\s+cell\s+carcinoma"),
        ("carcinosarcoma", r"carcinosarcoma"),
        ("adenocarcinoma", r"adenocarcinoma"),
        ("invasive carcinoma", r"invasive\s+carcinoma"),
        ("poorly differentiated carcinoma", r"poorly\s+differentiated\s+carcinoma"),
    ]
)

PRECANCER_PATTERNS = _compile_patterns(
    [
        ("复杂性增生伴不典型增生", r"复杂性增生伴不典型增生"),
        ("复杂性增生伴非典型增生", r"复杂性增生伴非典型增生"),
        ("复杂性非典型增生", r"复杂性非典型增生"),
        ("非典型子宫内膜增生", r"非典型子宫内膜增生"),
        ("不典型子宫内膜增生", r"不典型子宫内膜增生"),
        ("子宫内膜不典型增生", r"子宫内膜不典型增生"),
        ("非典型增生", r"非典型增生"),
        ("不典型增生", r"不典型增生"),
        ("EIN", r"\bEIN\b"),
        ("endometrial intraepithelial neoplasia", r"endometrial\s+intraepithelial\s+neoplasia"),
        ("atypical endometrial hyperplasia", r"atypical\s+endometrial\s+hyperplasia"),
        ("complex atypical hyperplasia", r"complex\s+atypical\s+hyperplasia"),
    ]
)

NEGATIVE_PATTERNS = _compile_patterns(
    [
        ("分泌期改变", r"分泌期改变"),
        ("分泌性改变", r"分泌性改变"),
        ("增生期改变", r"增生期改变"),
        ("增殖期改变", r"增殖期改变"),
        ("单纯性增生", r"单纯性增生"),
        ("复杂性增生", r"复杂性增生"),
        ("子宫内膜增殖症", r"子宫内膜增殖症"),
        ("子宫内膜增生", r"子宫内膜增生"),
        ("子宫内膜息肉", r"子宫内膜息肉"),
        ("息肉", r"息肉"),
        ("急慢性炎", r"急慢性炎"),
        ("慢性炎", r"慢性炎"),
        ("炎症", r"炎症"),
        ("平滑肌瘤", r"平滑肌瘤"),
        ("肌瘤", r"肌瘤"),
        ("良性囊肿", r"良性囊肿"),
        ("囊肿", r"囊肿"),
        ("未见明显异型", r"未见明显异型"),
        ("未见异型", r"未见异型"),
        ("萎缩性子宫内膜", r"萎缩性子宫内膜"),
        ("低级别上皮内瘤变", r"低级别上皮内瘤变"),
    ]
)

ENDOMETRIUM_PATTERNS = _compile_patterns(
    [
        ("子宫内膜", r"子宫内膜"),
        ("内膜", r"内膜"),
        ("endometrial", r"endometrial"),
        ("endometrium", r"endometrium"),
    ]
)

OTHER_ORGAN_PATTERNS = _compile_patterns(
    [
        ("宫颈", r"宫颈"),
        ("卵巢", r"卵巢"),
        ("输卵管", r"输卵管"),
        ("阴道", r"阴道"),
        ("外阴", r"外阴"),
        ("结肠", r"结肠|升结肠|降结肠|横结肠|乙状结肠"),
        ("直肠", r"直肠"),
        ("胃", r"胃"),
        ("肠", r"小肠|大肠"),
        ("肝", r"肝"),
        ("胆", r"胆"),
        ("胰", r"胰"),
        ("肺", r"肺"),
        ("乳腺", r"乳腺"),
        ("肾", r"肾"),
        ("膀胱", r"膀胱"),
        ("甲状腺", r"甲状腺"),
    ]
)

CERVICAL_SQUAMOUS_PATTERNS = _compile_patterns(
    [
        ("宫颈组织", r"宫颈组织"),
        ("宫颈", r"宫颈"),
        ("CIN", r"\bCIN(?:\d+)?\b"),
        ("HSIL", r"\bHSIL\b"),
        ("LSIL", r"\bLSIL\b"),
        ("鳞状上皮内病变", r"鳞状上皮内病变"),
        ("高级别鳞状上皮内病变", r"高级别鳞状上皮内病变"),
        ("低级别鳞状上皮内病变", r"低级别鳞状上皮内病变"),
        ("宫颈上皮内瘤变", r"宫颈上皮内瘤变"),
        ("原位癌", r"原位癌"),
    ]
)

ENDOMETRIAL_TARGET_PATTERNS = _compile_patterns(
    [
        ("子宫内膜癌", r"子宫内膜癌"),
        ("内膜癌", r"内膜癌"),
        ("子宫内膜样癌", r"子宫内膜样癌"),
        ("子宫内膜样腺癌", r"子宫内膜样腺癌"),
        ("endometrioid carcinoma", r"endometrioid\s+carcinoma"),
        ("serous carcinoma", r"serous\s+carcinoma"),
        ("clear cell carcinoma", r"clear\s+cell\s+carcinoma"),
        ("癌肉瘤", r"癌肉瘤"),
        ("carcinosarcoma", r"carcinosarcoma"),
        ("子宫内膜非典型增生", r"子宫内膜非典型增生"),
        ("非典型子宫内膜增生", r"非典型子宫内膜增生"),
        ("不典型子宫内膜增生", r"不典型子宫内膜增生"),
        ("子宫内膜不典型增生", r"子宫内膜不典型增生"),
        ("EIN", r"\bEIN\b"),
        ("endometrial intraepithelial neoplasia", r"endometrial\s+intraepithelial\s+neoplasia"),
        ("atypical endometrial hyperplasia", r"atypical\s+endometrial\s+hyperplasia"),
        ("complex atypical hyperplasia", r"complex\s+atypical\s+hyperplasia"),
    ]
)

NON_ENDOMETRIAL_SITE_PATTERNS = _compile_patterns(
    [
        ("卵巢", r"卵巢"),
        ("附件", r"附件"),
        ("输卵管", r"输卵管"),
        ("宫颈", r"宫颈"),
        ("结肠", r"结肠|升结肠|降结肠|横结肠|乙状结肠"),
        ("直肠", r"直肠"),
        ("肺", r"肺"),
        ("纤支镜", r"纤支镜"),
        ("胃", r"胃"),
        ("肝", r"肝"),
        ("乳腺", r"乳腺"),
    ]
)

MALIGNANCY_KEYWORDS_PATTERNS = _compile_patterns(
    [
        ("恶性肿瘤", r"恶性肿瘤"),
        ("浸润癌", r"浸润癌"),
        ("低分化癌", r"低分化癌"),
        ("腺癌", r"腺癌"),
        ("癌肉瘤", r"癌肉瘤"),
        ("浆液性癌", r"浆液性癌"),
        ("透明细胞癌", r"透明细胞癌"),
        ("癌", r"癌"),
        ("carcinoma", r"carcinoma"),
        ("adenocarcinoma", r"adenocarcinoma"),
        ("carcinosarcoma", r"carcinosarcoma"),
        ("malignant", r"malignan\w*"),
    ]
)

LOW_GRADE_PATTERN = re.compile(r"低级别上皮内瘤变|low[-\s]?grade\s+intraepithelial", re.IGNORECASE)


def _first_match(text: str, patterns: List[Tuple[str, re.Pattern]]) -> Tuple[str, str]:
    for name, pattern in patterns:
        m = pattern.search(text)
        if m:
            return name, m.group(0)
    return "", ""


def _strip_by_patterns(text: str, patterns: List[Tuple[str, re.Pattern]]) -> str:
    out = text
    for _, pattern in patterns:
        out = pattern.sub(" ", out)
    return out


def _collect_hits(text: str, patterns: List[Tuple[str, re.Pattern]], max_hits: int = 2) -> List[str]:
    hits: List[str] = []
    for _, pattern in patterns:
        for m in pattern.finditer(text):
            hit = m.group(0).strip()
            if hit and hit not in hits:
                hits.append(hit)
                if len(hits) >= max_hits:
                    return hits
    return hits


def _contains_any(text: str, patterns: List[Tuple[str, re.Pattern]]) -> bool:
    return any(pattern.search(text) for _, pattern in patterns)


def classify_diagnose(text) -> Tuple[str, str]:
    if pd.isna(text) or str(text).strip() == "":
        return "negative", "DIAGNOSE_DESC为空，默认negative"

    raw = str(text).strip()
    raw = re.sub(r"\s+", " ", raw)

    neg_pos_hits = _collect_hits(raw, NEGATED_POSITIVE_PATTERNS, max_hits=2)
    cleaned_for_positive = _strip_by_patterns(raw, NEGATED_POSITIVE_PATTERNS)

    neg_prec_hits = _collect_hits(raw, NEGATED_PRECANCER_PATTERNS, max_hits=1)
    cleaned_for_precancer = _strip_by_patterns(cleaned_for_positive, NEGATED_PRECANCER_PATTERNS)

    # 宫颈/鳞状上皮相关病变优先排除到negative（仅当未出现子宫内膜目标病变关键词）
    _, cervical_hit = _first_match(raw, CERVICAL_SQUAMOUS_PATTERNS)
    has_endometrial_target = _contains_any(cleaned_for_precancer, ENDOMETRIAL_TARGET_PATTERNS)
    if cervical_hit and not has_endometrial_target:
        return "negative", "宫颈/鳞状上皮相关病变，非子宫内膜目标病变，建议人工复核"

    # 非子宫内膜来源恶性病变：不纳入positive
    has_non_endometrial_site = _contains_any(raw, NON_ENDOMETRIAL_SITE_PATTERNS)
    has_malignancy_keywords = _contains_any(cleaned_for_positive, MALIGNANCY_KEYWORDS_PATTERNS)
    if has_non_endometrial_site and has_malignancy_keywords and not has_endometrial_target:
        return "negative", "非子宫内膜来源恶性病变，不纳入子宫内膜癌阳性，建议人工复核"

    # 2) positive-A：明确子宫内膜癌相关诊断
    _, pos_direct_hit = _first_match(cleaned_for_positive, POSITIVE_DIRECT_PATTERNS)
    if pos_direct_hit:
        return "positive", f"命中子宫内膜癌明确诊断关键词: {pos_direct_hit}"

    # 2) positive-B：子宫内膜/宫腔/刮宫上下文 + 明确恶性癌种
    _, pos_context_hit = _first_match(raw, POSITIVE_CONTEXT_PATTERNS)
    _, pos_malignant_hit = _first_match(cleaned_for_positive, POSITIVE_MALIGNANT_PATTERNS)
    if pos_context_hit and pos_malignant_hit:
        return "positive", f"命中子宫内膜上下文({pos_context_hit}) + 恶性关键词({pos_malignant_hit})"

    # 3) precancer：无明确癌时，匹配癌前/高危病变关键词（已去除否定片段）
    pre_name, pre_hit = _first_match(cleaned_for_precancer, PRECANCER_PATTERNS)
    if pre_hit:
        return "precancer", f"命中precancer关键词: {pre_hit}"

    # 4) negative：其余全部默认negative
    has_low_grade = LOW_GRADE_PATTERN.search(raw) is not None
    has_endometrium_context = _contains_any(raw, ENDOMETRIUM_PATTERNS)
    has_other_organ_context = _contains_any(raw, OTHER_ORGAN_PATTERNS)

    if has_low_grade and has_other_organ_context and not has_endometrium_context:
        return "negative", "非子宫内膜相关低级别上皮内瘤变，归为negative"

    neg_name, neg_hit = _first_match(raw, NEGATIVE_PATTERNS)
    if neg_hit:
        return "negative", f"命中negative关键词: {neg_hit}"

    if neg_pos_hits or neg_prec_hits:
        hit_text = (neg_pos_hits + neg_prec_hits)[0]
        return "negative", f"命中否定表达: {hit_text}，未发现明确癌/癌前病变"

    return "negative", "未命中positive/precancer关键词，默认negative"


def main() -> int:
    if not os.path.exists(INPUT_PATH):
        print(f"[错误] 输入文件不存在: {INPUT_PATH}")
        return 1

    try:
        df = pd.read_excel(INPUT_PATH, sheet_name=0, engine="openpyxl")
    except Exception as exc:
        print(f"[错误] 读取Excel失败: {exc}")
        return 1

    if DIAG_COL not in df.columns:
        print(f"[错误] 缺少列: {DIAG_COL}")
        print("[提示] 当前列名为:", list(df.columns))
        return 1

    classified = df[DIAG_COL].apply(classify_diagnose)
    df["label"] = classified.map(lambda x: x[0])
    df["label_reason"] = classified.map(lambda x: x[1])

    output_dir = os.path.dirname(OUTPUT_PATH)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    try:
        with pd.ExcelWriter(OUTPUT_PATH, engine="openpyxl") as writer:
            for sheet_name in ["positive", "precancer", "negative"]:
                df[df["label"] == sheet_name].to_excel(
                    writer,
                    sheet_name=sheet_name,
                    index=False,
                )
    except Exception as exc:
        print(f"[错误] 写出Excel失败: {exc}")
        return 1

    counts = df["label"].value_counts(dropna=False)
    print(f"输出文件已保存: {OUTPUT_PATH}")
    print(f"positive: {int(counts.get('positive', 0))}")
    print(f"precancer: {int(counts.get('precancer', 0))}")
    print(f"negative: {int(counts.get('negative', 0))}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
import os
from typing import Dict, List

import pandas as pd


LABELED_XLSX = "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/2025-8-4病理和B超报告withjpg_labeled.xlsx"
PATIENT_XLSX = "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid_public/by_sickid_public_per_patient.xlsx"

OUT_PATHOLOGY_XLSX = "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/by_sickid_public_per_patient_pathology_by_label.xlsx"
OUT_DICOM_XLSX = "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/by_sickid_public_per_patient_dicom_by_label.xlsx"

TARGET_LABELS = ["positive", "precancer", "negative"]
LABEL_PRIORITY = {"positive": 3, "precancer": 2, "negative": 1}


def normalize_label(x: str) -> str:
    return str(x).strip().lower()


def pick_id_col(columns: List[str]) -> str:
    candidates = ["SICK_ID", "sickid", "SICKID", "sick_id"]
    col_map = {str(c).strip().lower(): c for c in columns}
    for name in candidates:
        key = name.lower()
        if key in col_map:
            return col_map[key]
    return ""


def load_labeled_rows(path: str) -> pd.DataFrame:
    sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    rows: List[pd.DataFrame] = []

    for sheet_name, df in sheets.items():
        if df is None or df.empty:
            continue
        id_col = pick_id_col(list(df.columns))
        if not id_col:
            continue

        tmp = df.copy()
        tmp["sickid"] = tmp[id_col].astype(str).str.strip()

        if "label" in tmp.columns:
            tmp["label"] = tmp["label"].map(normalize_label)
        else:
            tmp["label"] = normalize_label(sheet_name)

        tmp = tmp[tmp["label"].isin(TARGET_LABELS)]
        tmp = tmp[tmp["sickid"] != ""]
        if not tmp.empty:
            rows.append(tmp[["sickid", "label"]])

    if not rows:
        return pd.DataFrame(columns=["sickid", "label"])
    return pd.concat(rows, ignore_index=True).drop_duplicates()


def resolve_patient_label(df: pd.DataFrame) -> Dict[str, str]:
    """
    同一 sickid 若出现多个标签，按优先级合并：
    positive > precancer > negative
    """
    if df.empty:
        return {}

    best: Dict[str, str] = {}
    for _, row in df.iterrows():
        sid = row["sickid"]
        lbl = row["label"]
        if sid not in best:
            best[sid] = lbl
            continue
        if LABEL_PRIORITY[lbl] > LABEL_PRIORITY[best[sid]]:
            best[sid] = lbl
    return best


def split_modality_by_label(modality_df: pd.DataFrame, sickid_to_label: Dict[str, str]) -> Dict[str, pd.DataFrame]:
    if modality_df is None or modality_df.empty:
        return {k: pd.DataFrame(columns=["sickid", "modality", "image_path"]) for k in TARGET_LABELS}

    df = modality_df.copy()
    if "sickid" not in df.columns:
        raise ValueError("by_sickid_public_per_patient.xlsx 缺少 sickid 列")

    df["sickid"] = df["sickid"].astype(str).str.strip()
    df = df[df["sickid"].isin(sickid_to_label.keys())].copy()
    if df.empty:
        return {k: df.copy() for k in TARGET_LABELS}

    df["label"] = df["sickid"].map(sickid_to_label)
    out: Dict[str, pd.DataFrame] = {}
    for label in TARGET_LABELS:
        part = df[df["label"] == label].copy()
        # 保持和 by_sickid_public_per_patient.xlsx 一样的列（不输出 label 列）
        part = part.drop(columns=["label"], errors="ignore")
        out[label] = part
    return out


def write_three_sheets(out_path: str, by_label: Dict[str, pd.DataFrame]) -> None:
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        for label in ["positive", "negative", "precancer"]:
            by_label.get(label, pd.DataFrame()).to_excel(writer, sheet_name=label, index=False)


def main() -> int:
    if not os.path.exists(LABELED_XLSX):
        print(f"[错误] 文件不存在: {LABELED_XLSX}")
        return 1
    if not os.path.exists(PATIENT_XLSX):
        print(f"[错误] 文件不存在: {PATIENT_XLSX}")
        return 1

    labeled_df = load_labeled_rows(LABELED_XLSX)
    if labeled_df.empty:
        print("[错误] 在 labeled 文件里没有找到可用的 sickid/label")
        return 1

    sickid_to_label = resolve_patient_label(labeled_df)

    patient_sheets = pd.read_excel(PATIENT_XLSX, sheet_name=None, engine="openpyxl")
    if "pathology" not in patient_sheets or "dicom" not in patient_sheets:
        print("[错误] by_sickid_public_per_patient.xlsx 必须包含 pathology 和 dicom 两个 sheet")
        print("[提示] 当前 sheet:", list(patient_sheets.keys()))
        return 1

    pathology_by_label = split_modality_by_label(patient_sheets["pathology"], sickid_to_label)
    dicom_by_label = split_modality_by_label(patient_sheets["dicom"], sickid_to_label)

    write_three_sheets(OUT_PATHOLOGY_XLSX, pathology_by_label)
    write_three_sheets(OUT_DICOM_XLSX, dicom_by_label)

    print(f"[完成] 输出: {OUT_PATHOLOGY_XLSX}")
    print("pathology counts:")
    print(f"  positive: {len(pathology_by_label['positive'])}")
    print(f"  negative: {len(pathology_by_label['negative'])}")
    print(f"  precancer: {len(pathology_by_label['precancer'])}")

    print(f"[完成] 输出: {OUT_DICOM_XLSX}")
    print("dicom counts:")
    print(f"  positive: {len(dicom_by_label['positive'])}")
    print(f"  negative: {len(dicom_by_label['negative'])}")
    print(f"  precancer: {len(dicom_by_label['precancer'])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

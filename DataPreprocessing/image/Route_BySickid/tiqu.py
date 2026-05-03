import os
import json
import shutil
import argparse
from pathlib import Path

import pandas as pd
from PIL import Image


def normalize_to_posix_rel(p: str) -> str:
    """
    把各种形式的路径统一成 ./MZ/... 或 ./ZY/... 的 posix 相对路径
    例如 .\\MZ\\a\\1.jpg -> ./MZ/a/1.jpg
    """
    if p is None:
        return ""
    p = str(p).strip().strip('"').strip("'")
    p = p.replace("\\", "/")

    if p.startswith("./"):
        pass
    elif p.startswith("."):
        # ".\MZ/..." -> "./MZ/..."
        p = p.lstrip(".")
        if not p.startswith("/"):
            p = "/" + p
        p = "." + p
    else:
        # "MZ/..." -> "./MZ/..."
        if not p.startswith("/"):
            p = "./" + p
        else:
            p = "." + p

    while "//" in p:
        p = p.replace("//", "/")
    return p


def get_folder_posix(file_posix: str) -> str:
    # "./MZ/xxx/1.jpg" -> "./MZ/xxx"
    if not file_posix:
        return ""
    parts = file_posix.rsplit("/", 1)
    return parts[0] if len(parts) == 2 else ""


def img_size(path: Path):
    # Rows=height, Columns=width
    try:
        with Image.open(path) as im:
            w, h = im.size
            return h, w
    except Exception:
        return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--excel", required=True, help="Excel路径，例如 data.xlsx")
    parser.add_argument("--src-root", required=True, help="原始图片根目录，例如 Path (里面有 MZ/ZY)")
    parser.add_argument("--dst-root", required=True, help="新输出根目录，例如 NewPath")
    parser.add_argument("--out-name", default="meta.json", help="meta文件名，默认 meta.json")
    args = parser.parse_args()

    excel_path = Path(args.excel)
    src_root = Path(args.src_root)
    dst_root = Path(args.dst_root)

    df = pd.read_excel(excel_path, engine="openpyxl")

    required_cols = ["DIAGNOSE_NAME", "VISIT_STATE_DESC", "DIAGNOSE_DESC", "File_Path"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Excel缺少列：{missing}，当前列={list(df.columns)}")

    # 标准化路径
    df["_file_posix"] = df["File_Path"].apply(normalize_to_posix_rel)
    df["_folder_posix"] = df["_file_posix"].apply(get_folder_posix)

    # 按“病例文件夹”分组：每组输出一个文件夹 + meta.json
    for folder_posix, g in df.groupby("_folder_posix"):
        if not folder_posix:
            continue

        # folder_posix like "./MZ/1807xxxx林素珠23080127"
        folder_rel = folder_posix[2:] if folder_posix.startswith("./") else folder_posix.lstrip("./")
        src_folder_abs = src_root / folder_rel
        dst_folder_abs = dst_root / folder_rel
        dst_folder_abs.mkdir(parents=True, exist_ok=True)

        # 先拷贝该组涉及到的所有图片（只拷 Excel 提到的）
        file_posix_list = sorted(set(g["_file_posix"].tolist()))
        copied_files = []

        for fp in file_posix_list:
            if not fp:
                continue
            rel = fp[2:] if fp.startswith("./") else fp.lstrip("./")
            src_file = src_root / rel
            dst_file = dst_root / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)

            if src_file.exists() and src_file.is_file():
                shutil.copy2(src_file, dst_file)
                copied_files.append(dst_file)
            else:
                # 找不到就跳过（也可以改成 raise）
                print(f"[WARN] missing source file: {src_file}")

        # meta字段（用该文件夹第一条记录）
        row0 = g.iloc[0]
        meta = {
            "diagnose_name": None if pd.isna(row0["DIAGNOSE_NAME"]) else str(row0["DIAGNOSE_NAME"]),
            "short_description": None if pd.isna(row0["VISIT_STATE_DESC"]) else str(row0["VISIT_STATE_DESC"]),
            "detail_description": None if pd.isna(row0["DIAGNOSE_DESC"]) else str(row0["DIAGNOSE_DESC"]),
            "image_info": []
        }

        # 生成 image_info（只写这次拷贝过去的图片）
        image_info = []
        for dst_file in sorted(copied_files, key=lambda x: x.name):
            rows, cols = img_size(dst_file)
            image_info.append({
                "png_path": f"{folder_posix}/{dst_file.name}",  # 例如 ./MZ/xxx/23080127_001.jpg
                "Rows": rows,
                "Columns": cols,
                "image_name": dst_file.name
            })
        meta["image_info"] = image_info

        # 写 meta.json 到 NewPath 对应病例文件夹
        out_path = dst_folder_abs / args.out_name
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        print(f"[OK] folder: {dst_folder_abs} | images: {len(image_info)} | meta: {out_path}")


if __name__ == "__main__":
    main()

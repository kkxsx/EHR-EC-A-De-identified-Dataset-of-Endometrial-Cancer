#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build a visual dataset view by SickID using symlinks (Linux-friendly).

Inputs:
- Excel with columns like:
    SICK_ID
    PACS_BILL_NO(Accession_Number)   (or PACS_BILL_NO*)
    File_Path                        (path ending with image filename)
- DICOM folder tree:
    dicom_root/{exam_id}/{accession}/(dicom files...)
- Pathology image folder:
    path_root/(many images, possibly nested)

Outputs (under --out_root):
- by_sickid/{SICK_ID}/dicom/{accession} -> symlink to real dicom accession dir
- by_sickid/{SICK_ID}/pathology/{image_name} -> symlink to real image file
- by_sickid/{SICK_ID}/meta.json
- dataset_index.csv (global overview)
- missing_accessions.csv / missing_images.csv (global missing lists)

Usage example:
python build_by_sickid.py \
  --excel "/data/2025-8-4病理和B超报告withjpg.xlsx" \
  --dicom_root "/data/dicom" \
  --path_root "/data/Test" \
  --out_root "/data/by_sickid" \
  --relative-links
"""

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build a visual dataset view by SickID using symlinks (Linux-friendly).
(Added tqdm progress bars for long loops.)
"""

import os
import re
import json
import argparse
from pathlib import Path
from collections import defaultdict
from decimal import Decimal
from datetime import datetime
from tqdm import tqdm  

# -----------------------------
# Helpers: normalize / extract
# -----------------------------
def normalize_accession(x) -> str:
    """AccessionNumber: keep as string; fix .0 and scientific notation."""
    if x is None:
        return ""
    s = str(x).strip()
    if not s or s.lower() in ("nan", "<na>"):
        return ""
    if re.fullmatch(r"\d+\.0+", s):  # 12345.0 -> 12345
        s = s.split(".")[0]
    if re.search(r"[eE]", s):  # scientific notation
        try:
            s2 = format(Decimal(s), "f").rstrip("0").rstrip(".")
            if s2:
                s = s2
        except Exception:
            pass
    return s


def norm_filename(name: str) -> str:
    """lowercase filename, strip quotes/spaces; treat <na>/nan as empty."""
    if name is None:
        return ""
    s = str(name).strip().strip('"').strip("'")
    if not s or s.lower() in ("nan", "<na>"):
        return ""
    return s.lower()


def extract_basename_from_file_path(cell: str) -> str:
    """From Excel File_Path -> basename (filename only), lowercase."""
    if cell is None:
        return ""
    s = str(cell).strip()
    if not s or s.lower() in ("nan", "<na>"):
        return ""
    s = re.sub(r"^[.][\\/]+", "", s)  # remove .\ or ./
    s = s.replace("\\", "/")          # normalize slashes
    base = s.split("/")[-1].strip()
    return norm_filename(base)


def pick_column(df, prefer_exact: str, contains_keywords=None):
    """Pick a column by exact (case-insensitive) or keyword containment."""
    cols = list(df.columns)
    lower_map = {str(c).strip().lower(): c for c in cols}
    key = prefer_exact.strip().lower()
    if key in lower_map:
        return lower_map[key]
    if contains_keywords:
        for c in cols:
            cl = str(c).strip().lower()
            if all(k.lower() in cl for k in contains_keywords):
                return c
        for c in cols:
            cl = str(c).strip().lower()
            if any(k.lower() in cl for k in contains_keywords):
                return c
    return None


def safe_symlink(src: Path, dst: Path, relative: bool, overwrite: bool, is_dir: bool):
    """Create symlink dst -> src (optionally relative)."""
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() or dst.is_symlink():
        if overwrite:
            try:
                if dst.is_dir() and not dst.is_symlink():
                    raise RuntimeError(f"Refusing to overwrite real directory: {dst}")
                dst.unlink()
            except Exception as e:
                raise RuntimeError(f"Failed to remove existing path {dst}: {e}")
        else:
            return

    target = src
    if relative:
        target = Path(os.path.relpath(src, start=dst.parent))

    dst.symlink_to(target, target_is_directory=is_dir)


# -----------------------------
# Index builders
# -----------------------------
def build_dicom_accession_index(dicom_root: Path):
    """Build accession -> list[accession_dir_path] by scanning dicom_root/{exam}/{accession}/..."""
    acc_to_paths = defaultdict(list)
    if not dicom_root.exists():
        raise FileNotFoundError(f"DICOM root not found: {dicom_root}")

    for exam_entry in dicom_root.iterdir():
        if not exam_entry.is_dir():
            continue
        for acc_entry in exam_entry.iterdir():
            if not acc_entry.is_dir():
                continue
            acc_norm = normalize_accession(acc_entry.name)
            if not acc_norm:
                continue
            acc_to_paths[acc_norm].append(acc_entry.resolve())
    return acc_to_paths


def build_pathology_image_index(path_root: Path):
    """Build image_name(lowercase) -> list[full_paths] by scanning path_root recursively."""
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
    name_to_paths = defaultdict(list)

    if not path_root.exists():
        raise FileNotFoundError(f"Pathology root not found: {path_root}")

    for root, _, files in os.walk(path_root):
        for fn in files:
            if fn.startswith("."):
                continue
            if Path(fn).suffix.lower() not in exts:
                continue
            key = norm_filename(fn)
            if not key:
                continue
            name_to_paths[key].append((Path(root) / fn).resolve())
    return name_to_paths


# -----------------------------
# Excel -> SickID mappings
# -----------------------------
def load_excel_mappings(excel_path: Path):
    """
    Return:
      sick_to_accessions: dict[sick_id] -> set(accession_norm)
      sick_to_images:     dict[sick_id] -> set(image_name_lower)
      sick_to_residence:  dict[sick_id] -> set(residence_no_string)  (optional)
      stats: dict
    """
    import pandas as pd

    df0 = pd.read_excel(excel_path)

    col_sick = pick_column(df0, prefer_exact="SICK_ID", contains_keywords=["sick", "id"])
    col_acc  = pick_column(df0, prefer_exact="PACS_BILL_NO", contains_keywords=["pacs", "bill", "no"])
    col_fp   = pick_column(df0, prefer_exact="File_Path", contains_keywords=["file", "path"])
    col_res  = pick_column(df0, prefer_exact="RESIDENCE_NO", contains_keywords=["residence", "no"])

    missing = [x for x in [("SICK_ID", col_sick), ("PACS_BILL_NO", col_acc), ("File_Path", col_fp)] if x[1] is None]
    if missing:
        raise ValueError(f"Missing required columns in Excel. Missing picks: {missing}. Columns={list(df0.columns)}")

    dtype_map = {col_sick: "string", col_acc: "string", col_fp: "string"}
    if col_res is not None:
        dtype_map[col_res] = "string"
    df = pd.read_excel(excel_path, dtype=dtype_map)

    sick_to_accessions = defaultdict(set)
    sick_to_images = defaultdict(set)
    sick_to_residence = defaultdict(set)

    invalid_sick = 0
    invalid_acc = 0
    invalid_imgname = 0
    invalid_res = 0

    # NEW: tqdm progress
    total_rows = len(df)
    for _, row in tqdm(df.iterrows(), total=total_rows, desc="Mapping Excel rows", unit="row"):
        sick = str(row[col_sick]).strip() if row[col_sick] is not None else ""
        if not sick or sick.lower() in ("nan", "<na>"):
            invalid_sick += 1
            continue

        acc = normalize_accession(row[col_acc])
        if acc:
            sick_to_accessions[sick].add(acc)
        else:
            invalid_acc += 1

        img = extract_basename_from_file_path(row[col_fp])
        if img:
            sick_to_images[sick].add(img)
        else:
            invalid_imgname += 1

        if col_res is not None:
            res = str(row[col_res]).strip() if row[col_res] is not None else ""
            if res and res.lower() not in ("nan", "<na>"):
                sick_to_residence[sick].add(res)
            else:
                invalid_res += 1

    stats = {
        "excel_path": str(excel_path),
        "excel_rows": int(len(df)),
        "excel_col_sick_used": str(col_sick),
        "excel_col_accession_used": str(col_acc),
        "excel_col_file_path_used": str(col_fp),
        "excel_col_residence_used": str(col_res) if col_res is not None else "",
        "excel_unique_sick_ids": int(len(set(sick_to_accessions.keys()) | set(sick_to_images.keys()) | set(sick_to_residence.keys()))),
        "excel_invalid_sick_rows": int(invalid_sick),
        "excel_invalid_accession_rows": int(invalid_acc),
        "excel_invalid_image_name_rows": int(invalid_imgname),
        "excel_invalid_residence_rows": int(invalid_res),
        "residence_no_enabled": bool(col_res is not None),
    }
    return sick_to_accessions, sick_to_images, sick_to_residence, stats


# -----------------------------
# Main build
# -----------------------------
def main():
    ap = argparse.ArgumentParser(description="Create by_sickid symlink view + per-sick meta.json + global index.")
    ap.add_argument("--excel", required=True, help="Excel path")
    ap.add_argument("--dicom_root", required=True, help="DICOM root: dicom/{exam}/{accession}/...")
    ap.add_argument("--path_root", required=True, help="Pathology images root (e.g. Test/)")
    ap.add_argument("--out_root", required=True, help="Output root for by_sickid")
    ap.add_argument("--relative-links", action="store_true", help="Create relative symlinks (more portable)")
    ap.add_argument("--overwrite-links", action="store_true", help="Overwrite existing links if present")
    ap.add_argument("--limit", type=int, default=0, help="Debug: only build first N SickIDs (0=all)")
    args = ap.parse_args()

    excel_path = Path(args.excel)
    dicom_root = Path(args.dicom_root)
    path_root = Path(args.path_root)
    out_root = Path(args.out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    print("[1/5] scanning dicom tree...")
    acc_to_paths = build_dicom_accession_index(dicom_root)
    print(f"  dicom unique accessions indexed: {len(acc_to_paths)}")

    print("[2/5] scanning pathology images...")
    img_to_paths = build_pathology_image_index(path_root)
    print(f"  pathology unique filenames indexed: {len(img_to_paths)}")

    print("[3/5] reading excel mappings...")
    sick_to_accessions, sick_to_images, sick_to_residence, excel_stats = load_excel_mappings(excel_path)

    all_sicks = sorted(set(sick_to_accessions.keys()) | set(sick_to_images.keys()) | set(sick_to_residence.keys()))
    if args.limit and args.limit > 0:
        all_sicks = all_sicks[: args.limit]
    print(f"  sick ids to build: {len(all_sicks)}")

    if excel_stats["residence_no_enabled"]:
        print(f"  RESIDENCE_NO enabled, column used: {excel_stats['excel_col_residence_used']}")
    else:
        print("  RESIDENCE_NO not found (optional), will store empty list per sick.")

    all_excel_images = set()
    for s in sick_to_images.keys():
        all_excel_images.update(sick_to_images[s])

    print("[4/5] building by_sickid view...")
    global_rows = []
    missing_accessions_rows = []
    missing_images_rows = []
    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    for sick in tqdm(all_sicks, desc="Building SickID folders", unit="sick"):
        sick_dir = out_root / str(sick)
        dicom_out = sick_dir / "dicom"
        path_out = sick_dir / "pathology"

        accs = sorted(sick_to_accessions.get(sick, set()))
        imgs = sorted(sick_to_images.get(sick, set()))
        res_list = sorted(sick_to_residence.get(sick, set()))

        linked_acc = []
        missing_acc = []
        ambiguous_acc = []

        for acc in accs:
            paths = acc_to_paths.get(acc, [])
            if not paths:
                missing_acc.append(acc)
                continue
            if len(paths) > 1:
                ambiguous_acc.append({"accession": acc, "candidates": [str(p) for p in paths]})
            src_dir = paths[0]
            dst_dir = dicom_out / acc
            safe_symlink(src_dir, dst_dir, relative=args.relative_links, overwrite=args.overwrite_links, is_dir=True)
            linked_acc.append({"accession": acc, "link_path": str(dst_dir), "target_path": str(src_dir)})

        linked_imgs = []
        missing_img = []
        ambiguous_img = []

        for img in imgs:
            paths = img_to_paths.get(img, [])
            if not paths:
                missing_img.append(img)
                continue
            if len(paths) > 1:
                ambiguous_img.append({"image_name": img, "candidates": [str(p) for p in paths]})
            src_file = paths[0]
            dst_file = path_out / Path(img).name
            safe_symlink(src_file, dst_file, relative=args.relative_links, overwrite=args.overwrite_links, is_dir=False)
            linked_imgs.append({"image_name": img, "link_path": str(dst_file), "target_path": str(src_file)})

        has_dicom = int(len(linked_acc) > 0)
        has_path = int(len(linked_imgs) > 0)

        meta = {
            "sick_id": str(sick),
            "residence_no": res_list,
            "created_at_utc": now,
            "source": {
                "excel_path": str(excel_path),
                "dicom_root": str(dicom_root),
                "pathology_root": str(path_root),
                "link_type": "symlink",
                "relative_links": bool(args.relative_links),
            },
            "dicom": {
                "accessions_from_excel": accs,
                "linked_accessions": linked_acc,
                "missing_accessions": missing_acc,
                "ambiguous_accessions": ambiguous_acc,
            },
            "pathology": {
                "image_names_from_excel": imgs,
                "linked_images": linked_imgs,
                "missing_images": missing_img,
                "ambiguous_images": ambiguous_img,
            },
            "counts": {
                "num_accessions_from_excel": len(accs),
                "num_accessions_linked": len(linked_acc),
                "num_accessions_missing": len(missing_acc),
                "num_images_from_excel": len(imgs),
                "num_images_linked": len(linked_imgs),
                "num_images_missing": len(missing_img),
                "num_residence_no": len(res_list),
                "has_dicom": has_dicom,
                "has_pathology": has_path,
                "has_both": int(has_dicom and has_path),
            },
        }

        sick_dir.mkdir(parents=True, exist_ok=True)
        (sick_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        global_rows.append({
            "sick_id": str(sick),
            "num_residence_no": len(res_list),
            "has_dicom": has_dicom,
            "has_pathology": has_path,
            "has_both": int(has_dicom and has_path),
            "num_accessions_from_excel": len(accs),
            "num_accessions_linked": len(linked_acc),
            "num_accessions_missing": len(missing_acc),
            "num_images_from_excel": len(imgs),
            "num_images_linked": len(linked_imgs),
            "num_images_missing": len(missing_img),
        })

        for acc in missing_acc:
            missing_accessions_rows.append({"sick_id": str(sick), "accession": acc})
        for img in missing_img:
            missing_images_rows.append({"sick_id": str(sick), "image_name": img})

    print("[5/5] writing global outputs...")
    import pandas as pd

    pd.DataFrame([excel_stats]).to_csv(out_root / "excel_stats.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(global_rows).to_csv(out_root / "dataset_index.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(missing_accessions_rows).to_csv(out_root / "missing_accessions.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(missing_images_rows).to_csv(out_root / "missing_images.csv", index=False, encoding="utf-8-sig")

    unref_rows = []
    for img_name, paths in img_to_paths.items():
        if img_name not in all_excel_images:
            unref_rows.append({
                "image_name": img_name,
                "num_paths": len(paths),
                "example_path": str(paths[0]) if paths else "",
            })
    pd.DataFrame(unref_rows).to_csv(out_root / "unreferenced_images.csv", index=False, encoding="utf-8-sig")

    idx = pd.DataFrame(global_rows)
    dicom_only = int(((idx["has_dicom"] == 1) & (idx["has_pathology"] == 0)).sum())
    path_only = int(((idx["has_dicom"] == 0) & (idx["has_pathology"] == 1)).sum())

    summary = {
        "total_sickids_built": int(len(idx)),
        "sickids_has_dicom": int(idx["has_dicom"].sum()),
        "sickids_has_pathology": int(idx["has_pathology"].sum()),
        "sickids_has_both": int(idx["has_both"].sum()),
        "sickids_dicom_only": dicom_only,
        "sickids_pathology_only": path_only,
        "total_accessions_linked": int(idx["num_accessions_linked"].sum()),
        "total_images_linked": int(idx["num_images_linked"].sum()),
        "total_missing_accessions": int(idx["num_accessions_missing"].sum()),
        "total_missing_images": int(idx["num_images_missing"].sum()),
        "unreferenced_images_count": int(len(unref_rows)),
    }
    (out_root / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nDone.")
    print(f"- by_sickid root: {out_root}")
    print(f"- dataset_index.csv: {out_root / 'dataset_index.csv'}")
    print(f"- excel_stats.csv: {out_root / 'excel_stats.csv'}")
    print(f"- missing_accessions.csv: {out_root / 'missing_accessions.csv'}")
    print(f"- missing_images.csv: {out_root / 'missing_images.csv'}")
    print(f"- unreferenced_images.csv: {out_root / 'unreferenced_images.csv'}")
    print(f"- per-sick meta.json: {out_root}/<SICK_ID>/meta.json")
    print(f"- summary.json: {out_root / 'summary.json'}")


if __name__ == "__main__":
    main()

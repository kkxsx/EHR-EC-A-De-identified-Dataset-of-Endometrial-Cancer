#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Patch unreferenced pathology images into by_sickid view by RESIDENCE_NO.

Inputs:
- unreferenced_images.csv:
    must contain at least: example_path
    optional: num_paths, image_name
- mapping_excel (more complete table):
    contains columns like SICK_ID and RESIDENCE_NO
    (no accession, no filepath needed)

Logic:
1) For each row in unreferenced_images.csv, take example_path (a file path).
2) Extract RESIDENCE_NO from the parent folder name:
      .../Path/MZ/5492558017黄丽梅1506701/1506701_001.jpg
   parent folder basename: 5492558017黄丽梅1506701
   RESIDENCE_NO = leading digits => 5492558017
3) Lookup RESIDENCE_NO -> one or more SICK_ID(s) using mapping_excel.
4) Create symlink:
      out_root/<SICK_ID>/pathology_unreferenced/<parent_folder_name> -> <real_parent_folder>
   (directory symlink, brings all images under that folder)

Outputs (under out_root):
- patched_unreferenced_links.csv
- patched_unreferenced_missing_residence.csv
- patched_unreferenced_ambiguous_residence.csv

Usage:
python patch_unreferenced_by_residence.py \
  --unref_csv "By_Sickid_try/unreferenced_images.csv" \
  --map_excel "更全SICKID_RESIDENCE.xlsx" \
  --out_root "By_Sickid_try" \
  --relative-links
"""

import os
import re
import argparse
from pathlib import Path
from collections import defaultdict
from decimal import Decimal

def normalize_number_like(x) -> str:
    """Keep id-like numbers as string; fix .0 and scientific notation."""
    if x is None:
        return ""
    s = str(x).strip()
    if not s or s.lower() in ("nan", "<na>"):
        return ""
    if re.fullmatch(r"\d+\.0+", s):  # 12345.0 -> 12345
        s = s.split(".")[0]
    if re.search(r"[eE]", s):
        try:
            s2 = format(Decimal(s), "f").rstrip("0").rstrip(".")
            if s2:
                s = s2
        except Exception:
            pass
    return s.strip()

def pick_column(df, prefer_exact: str, contains_keywords=None):
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
    dst.parent.mkdir(parents=True, exist_ok=True)

    if dst.exists() or dst.is_symlink():
        if overwrite:
            # refuse overwriting a real directory (not symlink)
            if dst.is_dir() and not dst.is_symlink():
                raise RuntimeError(f"Refusing to overwrite real directory: {dst}")
            dst.unlink()
        else:
            return False  # skipped

    target = src
    if relative:
        target = Path(os.path.relpath(src, start=dst.parent))

    dst.symlink_to(target, target_is_directory=is_dir)
    return True  # created

def extract_residence_no_from_example_path(example_path: str) -> str:
    """
    Given .../5492558017黄丽梅1506701/1506701_001.jpg
    Extract leading digits from parent directory name => 5492558017
    """
    if not example_path:
        return ""
    p = Path(str(example_path).strip())
    parent = p.parent.name
    m = re.match(r"^(\d+)", parent)
    return m.group(1) if m else ""

def load_residence_mapping(map_excel: Path):
    import pandas as pd

    df0 = pd.read_excel(map_excel)

    col_sick = pick_column(df0, prefer_exact="SICK_ID", contains_keywords=["sick", "id"])
    col_res  = pick_column(df0, prefer_exact="RESIDENCE_NO", contains_keywords=["residence", "no"])

    missing = [x for x in [("SICK_ID", col_sick), ("RESIDENCE_NO", col_res)] if x[1] is None]
    if missing:
        raise ValueError(f"Missing required columns in mapping excel. Missing picks: {missing}. Columns={list(df0.columns)}")

    df = pd.read_excel(map_excel, dtype={col_sick: "string", col_res: "string"})

    residence_to_sicks = defaultdict(set)
    sick_to_residences = defaultdict(set)

    for _, row in df.iterrows():
        sick = str(row[col_sick]).strip() if row[col_sick] is not None else ""
        res  = normalize_number_like(row[col_res])

        if not sick or sick.lower() in ("nan", "<na>"):
            continue
        if not res:
            continue

        residence_to_sicks[res].add(sick)
        sick_to_residences[sick].add(res)

    stats = {
        "map_excel": str(map_excel),
        "col_sick_used": str(col_sick),
        "col_residence_used": str(col_res),
        "unique_sick_ids": len(sick_to_residences),
        "unique_residence_nos": len(residence_to_sicks),
    }
    return residence_to_sicks, sick_to_residences, stats

def main():
    ap = argparse.ArgumentParser(description="Patch unreferenced pathology images to by_sickid using RESIDENCE_NO reverse lookup.")
    ap.add_argument("--unref_csv", required=True, help="unreferenced_images.csv path (must contain example_path column)")
    ap.add_argument("--map_excel", required=True, help="Mapping excel with SICK_ID and RESIDENCE_NO")
    ap.add_argument("--out_root", required=True, help="Existing by_sickid root (same as build output root)")
    ap.add_argument("--relative-links", action="store_true", help="Create relative symlinks (portable if moved together)")
    ap.add_argument("--overwrite-links", action="store_true", help="Overwrite existing links if present")
    ap.add_argument("--limit", type=int, default=0, help="Debug: only patch first N rows (0=all)")
    args = ap.parse_args()

    unref_csv = Path(args.unref_csv)
    map_excel = Path(args.map_excel)
    out_root  = Path(args.out_root)

    if not unref_csv.exists():
        raise FileNotFoundError(f"unref_csv not found: {unref_csv}")
    if not map_excel.exists():
        raise FileNotFoundError(f"map_excel not found: {map_excel}")
    out_root.mkdir(parents=True, exist_ok=True)

    # tqdm optional
    try:
        from tqdm import tqdm
    except Exception:
        tqdm = None

    # load mapping
    residence_to_sicks, _, map_stats = load_residence_mapping(map_excel)
    print(f"[1/3] loaded mapping: {map_stats}")

    # load unreferenced csv
    import pandas as pd
    df0 = pd.read_csv(unref_csv, dtype="string", encoding="utf-8", engine="python")
    # find example_path column
    col_example = pick_column(df0, prefer_exact="example_path", contains_keywords=["example", "path"])
    if col_example is None:
        raise ValueError(f"unreferenced csv missing example_path column. Columns={list(df0.columns)}")

    rows = df0.to_dict("records")
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    it = rows
    if tqdm is not None:
        it = tqdm(rows, desc="[2/3] patching", unit="row")

    patched = []
    missing_residence = []
    ambiguous_residence = []

    for r in it:
        example_path = str(r.get(col_example, "") or "").strip()
        if not example_path:
            missing_residence.append({
                "example_path": "",
                "reason": "empty_example_path",
            })
            continue

        p = Path(example_path)
        parent_dir = p.parent
        parent_name = parent_dir.name

        residence_no = extract_residence_no_from_example_path(example_path)
        if not residence_no:
            missing_residence.append({
                "example_path": example_path,
                "reason": "cannot_extract_residence_from_parent_dir",
                "parent_dir": str(parent_dir),
                "parent_name": parent_name,
            })
            continue

        sick_candidates = sorted(residence_to_sicks.get(residence_no, []))
        if not sick_candidates:
            missing_residence.append({
                "example_path": example_path,
                "reason": "residence_not_in_mapping",
                "residence_no": residence_no,
                "parent_dir": str(parent_dir),
            })
            continue

        chosen_sick = sick_candidates[0]
        if len(sick_candidates) > 1:
            ambiguous_residence.append({
                "residence_no": residence_no,
                "chosen_sick": chosen_sick,
                "candidates": "|".join(sick_candidates),
                "example_path": example_path,
                "parent_dir": str(parent_dir),
            })

        # link whole folder into that sickid
        dst = out_root / str(chosen_sick) / "pathology_unreferenced" / parent_name
        created = safe_symlink(
            src=parent_dir.resolve(),
            dst=dst,
            relative=args.relative_links,
            overwrite=args.overwrite_links,
            is_dir=True
        )

        patched.append({
            "residence_no": residence_no,
            "sick_id": str(chosen_sick),
            "parent_folder_name": parent_name,
            "example_path": example_path,
            "link_path": str(dst),
            "target_path": str(parent_dir.resolve()),
            "created": int(bool(created)),
            "num_sick_candidates": len(sick_candidates),
        })

    print("[3/3] writing reports...")
    pd.DataFrame(patched).to_csv(out_root / "patched_unreferenced_links.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(missing_residence).to_csv(out_root / "patched_unreferenced_missing_residence.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(ambiguous_residence).to_csv(out_root / "patched_unreferenced_ambiguous_residence.csv", index=False, encoding="utf-8-sig")

    print("\nDone.")
    print(f"- patched links: {out_root / 'patched_unreferenced_links.csv'}")
    print(f"- missing residence/mapping: {out_root / 'patched_unreferenced_missing_residence.csv'}")
    print(f"- ambiguous residence->sick: {out_root / 'patched_unreferenced_ambiguous_residence.csv'}")

if __name__ == "__main__":
    main()

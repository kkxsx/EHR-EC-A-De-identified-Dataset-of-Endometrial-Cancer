#!/usr/bin/env python3
"""
Build image relation tables for By_Sickid_public.

Outputs (in --out-dir):
1) per-image table:
   - by_sickid_public_per_image.csv (all modalities mixed)
   - by_sickid_public_per_image.xlsx (2 sheets: pathology, dicom)
2) per-patient aggregated table:
   - by_sickid_public_per_patient.csv (row = sickid + modality)
   - by_sickid_public_per_patient.xlsx (2 sheets: pathology, dicom)

Columns:
  sickid, modality, image_path

Path processing:
  remove the prefix in --trim-prefix from absolute image path.
  Example:
    /media/dell/.../data_med/By_Sickid_public/1000000313/dicom/a.jpg
  -> /By_Sickid_public/1000000313/dicom/a.jpg
"""

import argparse
import csv
import os
from collections import defaultdict
from pathlib import Path
from typing import DefaultDict, Dict, Iterable, List, Tuple


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MODALITIES = ("pathology", "dicom")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build relation tables for By_Sickid_public images.")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid_public"
        ),
        help="Root of By_Sickid_public.",
    )
    parser.add_argument(
        "--trim-prefix",
        type=Path,
        default=Path("/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med"),
        help="Prefix removed from absolute image paths.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/home/dsy"),
        help="Output directory.",
    )
    return parser.parse_args()


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(str(root), followlinks=False):
        # Skip hidden/system folders.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        base = Path(dirpath)
        for name in filenames:
            p = base / name
            if p.is_file():
                yield p


def detect_modality(p: Path) -> str:
    parent = p.parent.name.lower()
    if parent == "dicom":
        return "dicom"
    if parent.startswith("pathology"):
        return "pathology"
    return ""


def extract_sickid(p: Path, root: Path) -> str:
    rel_parts = p.relative_to(root).parts
    if len(rel_parts) == 0:
        return ""
    return rel_parts[0]


def trim_path(abs_path: Path, trim_prefix: Path) -> str:
    abs_posix = abs_path.resolve().as_posix()
    prefix = trim_prefix.resolve().as_posix().rstrip("/")
    if abs_posix.startswith(prefix):
        out = abs_posix[len(prefix) :]
        return out if out.startswith("/") else "/" + out
    return abs_posix


def collect_rows(root: Path, trim_prefix: Path) -> Tuple[List[Dict[str, str]], Dict[str, List[Dict[str, str]]]]:
    all_rows: List[Dict[str, str]] = []
    by_modality: Dict[str, List[Dict[str, str]]] = {m: [] for m in MODALITIES}

    for p in iter_files(root):
        if p.suffix.lower() not in IMAGE_EXTS:
            continue
        rel_parts = p.relative_to(root).parts
        # Exclude backups/trash folders.
        if ".crop_backups" in rel_parts or ".delete_trash" in rel_parts:
            continue

        modality = detect_modality(p)
        if modality not in MODALITIES:
            continue

        sickid = extract_sickid(p, root)
        if not sickid:
            continue

        row = {
            "sickid": sickid,
            "modality": modality,
            "image_path": trim_path(p, trim_prefix),
        }
        all_rows.append(row)
        by_modality[modality].append(row)

    all_rows.sort(key=lambda r: (r["sickid"], r["modality"], r["image_path"]))
    for m in MODALITIES:
        by_modality[m].sort(key=lambda r: (r["sickid"], r["image_path"]))
    return all_rows, by_modality


def aggregate_per_patient(by_modality: Dict[str, List[Dict[str, str]]]) -> Tuple[List[Dict[str, str]], Dict[str, List[Dict[str, str]]]]:
    grouped_all: List[Dict[str, str]] = []
    grouped_by_modality: Dict[str, List[Dict[str, str]]] = {m: [] for m in MODALITIES}

    for modality in MODALITIES:
        bucket: DefaultDict[str, List[str]] = defaultdict(list)
        for row in by_modality[modality]:
            bucket[row["sickid"]].append(row["image_path"])

        for sickid in sorted(bucket.keys()):
            paths = sorted(bucket[sickid])
            grouped_row = {
                "sickid": sickid,
                "modality": modality,
                # One cell per patient+modality containing all image paths.
                "image_path": "\n".join(paths),
            }
            grouped_by_modality[modality].append(grouped_row)
            grouped_all.append(grouped_row)

    grouped_all.sort(key=lambda r: (r["sickid"], r["modality"]))
    return grouped_all, grouped_by_modality


def write_csv(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["sickid", "modality", "image_path"])
        writer.writeheader()
        writer.writerows(rows)


def write_xlsx(path: Path, by_modality_rows: Dict[str, List[Dict[str, str]]]) -> None:
    try:
        from openpyxl import Workbook  # type: ignore
    except Exception as e:
        raise RuntimeError(
            "openpyxl is required for xlsx output. Install with: python3 -m pip install openpyxl"
        ) from e

    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    first = True
    for modality in MODALITIES:
        if first:
            ws = wb.active
            ws.title = modality
            first = False
        else:
            ws = wb.create_sheet(title=modality)

        ws.append(["sickid", "modality", "image_path"])
        for row in by_modality_rows.get(modality, []):
            ws.append([row["sickid"], row["modality"], row["image_path"]])

    wb.save(str(path))


def main() -> int:
    args = parse_args()
    root = args.root
    trim_prefix = args.trim_prefix
    out_dir = args.out_dir

    if not root.exists():
        print("[error] root not found:", root)
        return 2

    print("[info] root:", root)
    print("[info] trim_prefix:", trim_prefix)
    print("[info] out_dir:", out_dir)
    print("[info] scanning images...")

    all_rows, by_modality = collect_rows(root, trim_prefix)
    grouped_all, grouped_by_modality = aggregate_per_patient(by_modality)

    print(
        "[info] counts - per_image: total={}, pathology={}, dicom={}".format(
            len(all_rows), len(by_modality["pathology"]), len(by_modality["dicom"])
        )
    )
    print(
        "[info] counts - per_patient: total_rows={}, pathology_patients={}, dicom_patients={}".format(
            len(grouped_all), len(grouped_by_modality["pathology"]), len(grouped_by_modality["dicom"])
        )
    )

    per_image_csv = out_dir / "by_sickid_public_per_image.csv"
    per_patient_csv = out_dir / "by_sickid_public_per_patient.csv"
    per_image_xlsx = out_dir / "by_sickid_public_per_image.xlsx"
    per_patient_xlsx = out_dir / "by_sickid_public_per_patient.xlsx"

    write_csv(per_image_csv, all_rows)
    write_csv(per_patient_csv, grouped_all)
    print("[ok] wrote:", per_image_csv)
    print("[ok] wrote:", per_patient_csv)

    try:
        write_xlsx(per_image_xlsx, by_modality)
        write_xlsx(per_patient_xlsx, grouped_by_modality)
        print("[ok] wrote:", per_image_xlsx)
        print("[ok] wrote:", per_patient_xlsx)
    except RuntimeError as e:
        print("[warn]", e)
        print("[warn] csv outputs are ready; xlsx skipped.")

    print("[done]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


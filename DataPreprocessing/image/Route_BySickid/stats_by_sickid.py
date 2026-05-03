#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import argparse
from pathlib import Path

IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

def is_image_path(p: Path) -> bool:
    return p.suffix.lower() in IMG_EXTS

def count_images_under_dir(real_dir: Path) -> int:
    """Count image files under a real directory recursively."""
    n = 0
    for root, _, files in os.walk(real_dir):
        for fn in files:
            if fn.startswith("."):
                continue
            if is_image_path(Path(fn)):
                n += 1
    return n

def count_files_under_dir(real_dir: Path, only_dcm: bool) -> int:
    """Count files under a real directory recursively (optionally only .dcm)."""
    n = 0
    for root, _, files in os.walk(real_dir):
        for fn in files:
            if fn.startswith("."):
                continue
            if only_dcm and Path(fn).suffix.lower() != ".dcm":
                continue
            n += 1
    return n

def main():
    ap = argparse.ArgumentParser(description="Stats for By_Sickid view (count patients by folders; follow symlinks).")
    ap.add_argument("--root", required=True, help="By_Sickid root")
    ap.add_argument("--only-dcm", action="store_true", help="Count only .dcm under DICOM accession dirs")
    ap.add_argument("--count-dicom-files", action="store_true",
                    help="Follow dicom accession symlinks and count files inside (can be slow)")
    ap.add_argument("--include-unref", action="store_true",
                    help="Also count images under pathology_unreferenced (dir symlinks)")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        raise FileNotFoundError(root)

    # 病人 = root 下所有一级目录（你说的“有多少个文件夹就行”）
    sick_dirs = sorted([p for p in root.iterdir() if p.is_dir()])
    total_patients = len(sick_dirs)

    patients_has_path = 0
    patients_has_dicom = 0
    patients_has_both = 0

    # ---- Path ----
    # pathology 下通常是“单图软链”：By_Sickid/<sick>/pathology/<img> -> real_file
    path_view_entries = 0           # pathology 下条目数（通常就是图链接数）
    path_images_resolved = 0        # 解析软链后，确认为图的数量（更准确一点）

    # ---- Unref Path (dir symlink) ----
    unref_dir_links = 0
    unref_images_total = 0

    # ---- DICOM ----
    dicom_accession_links = 0
    dicom_files_total = 0

    for sick_dir in sick_dirs:
        p_path = sick_dir / "pathology"
        p_dicom = sick_dir / "dicom"
        p_unref = sick_dir / "pathology_unreferenced"

        this_has_path = False
        this_has_dicom = False

        # --- pathology (single-file links) ---
        if p_path.exists() and p_path.is_dir():
            for f in p_path.iterdir():
                # 统计“条目”，不管是不是坏链
                if f.is_file() or f.is_symlink():
                    path_view_entries += 1
                    # 尝试解析到真实目标并确认是图片
                    try:
                        tgt = f.resolve(strict=True)
                        if tgt.is_file() and is_image_path(tgt):
                            path_images_resolved += 1
                            this_has_path = True
                    except Exception:
                        # 断链/无权限/目标不存在，都跳过解析计数
                        pass

        # --- pathology_unreferenced (dir links -> count images inside) ---
        if args.include_unref and p_unref.exists() and p_unref.is_dir():
            for d in p_unref.iterdir():
                if d.is_symlink() or d.is_dir():
                    unref_dir_links += 1
                    try:
                        real_dir = d.resolve(strict=True)
                        if real_dir.is_dir():
                            cnt = count_images_under_dir(real_dir)
                            if cnt > 0:
                                this_has_path = True  # 也算“有Path”
                            unref_images_total += cnt
                    except Exception:
                        pass

        # --- dicom accession dirs (usually symlink dirs) ---
        if p_dicom.exists() and p_dicom.is_dir():
            for acc in p_dicom.iterdir():
                if acc.is_dir() or acc.is_symlink():
                    dicom_accession_links += 1
                    this_has_dicom = True

                    if args.count_dicom_files:
                        try:
                            real_dir = acc.resolve(strict=True)
                            if real_dir.is_dir():
                                dicom_files_total += count_files_under_dir(real_dir, only_dcm=args.only_dcm)
                        except Exception:
                            pass

        if this_has_path:
            patients_has_path += 1
        if this_has_dicom:
            patients_has_dicom += 1
        if this_has_path and this_has_dicom:
            patients_has_both += 1

    print("==== By_Sickid stats (folder-based patients) ====")
    print(f"root: {root}")
    print(f"patients_total(folders): {total_patients}")
    print(f"patients_has_path:       {patients_has_path}")
    print(f"patients_has_dicom:      {patients_has_dicom}")
    print(f"patients_has_both:       {patients_has_both}")

    print("\n---- Pathology ----")
    print(f"path_view_entries(*/pathology/*):    {path_view_entries}")
    print(f"path_images_resolved(valid images): {path_images_resolved}")

    if args.include_unref:
        print("\n---- Pathology unreferenced ----")
        print(f"unref_dir_links(*/pathology_unreferenced/*): {unref_dir_links}")
        print(f"unref_images_total(follow dir links):        {unref_images_total}")

    print("\n---- DICOM ----")
    print(f"dicom_accession_links(*/dicom/*): {dicom_accession_links}")
    if args.count_dicom_files:
        print(f"dicom_files_total(follow links):  {dicom_files_total}  (only_dcm={bool(args.only_dcm)})")
    else:
        print("dicom_files_total: (skip; add --count-dicom-files to enable)")

if __name__ == "__main__":
    main()

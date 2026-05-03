#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
from pathlib import Path
import argparse
from typing import List, Optional

SICKID_RE = re.compile(r"^\d{10}$")

# 固定 root 为你的真实路径（按你发的路径写）
ROOT = Path("/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid")


def find_sickid_by_filename(filename: str) -> List[str]:
    filename = Path(filename).name  # 只用 basename
    if not ROOT.exists():
        raise FileNotFoundError(f"ROOT not found: {ROOT}")

    found: List[str] = []

    # 关键：followlinks=True，dicom / pathology_unreferenced 很可能是软链接
    for dirpath, dirnames, filenames in os.walk(ROOT, followlinks=True):
        if filename in filenames:
            p = Path(dirpath) / filename

            sickid: Optional[str] = None
            for parent in p.parents:
                if SICKID_RE.match(parent.name):
                    sickid = parent.name
                    break

            if sickid:
                found.append(sickid)

    # 去重保持顺序
    dedup, seen = [], set()
    for x in found:
        if x not in seen:
            dedup.append(x)
            seen.add(x)
    return dedup


def list_folders_by_sickid(sickid: str) -> List[str]:
    if not SICKID_RE.match(sickid):
        raise ValueError("sickid must be 10 digits")

    base = ROOT / sickid
    if not base.exists():
        return []

    out = []
    for child in base.iterdir():
        if child.is_dir():
            out.append(str(child))
    return sorted(out)


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--name", help="filename like 1625211_001.jpg")
    group.add_argument("--sickid", help="10-digit sickid")

    args = parser.parse_args()

    if args.name:
        ids = find_sickid_by_filename(args.name)
        if not ids:
            print("NOT_FOUND")
        elif len(ids) == 1:
            print(ids[0])
        else:
            print("MULTIPLE_MATCHES:")
            for x in ids:
                print(x)

    if args.sickid:
        folders = list_folders_by_sickid(args.sickid)
        if not folders:
            print("NOT_FOUND")
        else:
            for f in folders:
                print(f)


if __name__ == "__main__":
    main()

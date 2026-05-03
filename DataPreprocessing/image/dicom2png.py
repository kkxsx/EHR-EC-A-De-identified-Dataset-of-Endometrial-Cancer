#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import os
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor
from functools import partial

import numpy as np
import pydicom
from PIL import Image
from tqdm import tqdm

try:
    from pydicom.pixel_data_handlers.util import apply_voi_lut, apply_modality_lut
except Exception:
    apply_voi_lut = None
    apply_modality_lut = None
from collections import Counter, defaultdict


def to_uint8(img: np.ndarray, ds) -> np.ndarray:
    arr = img.astype(np.float32)

    # Modality LUT / Rescale
    if apply_modality_lut is not None:
        try:
            arr = apply_modality_lut(arr, ds).astype(np.float32)
        except Exception:
            pass
    else:
        slope = float(getattr(ds, "RescaleSlope", 1.0) or 1.0)
        intercept = float(getattr(ds, "RescaleIntercept", 0.0) or 0.0)
        arr = arr * slope + intercept

    # VOI LUT / Window
    if apply_voi_lut is not None:
        try:
            arr = apply_voi_lut(arr, ds).astype(np.float32)
        except Exception:
            pass
    else:
        wc = getattr(ds, "WindowCenter", None)
        ww = getattr(ds, "WindowWidth", None)
        try:
            if wc is not None and ww is not None:
                if isinstance(wc, (list, tuple)):
                    wc = float(wc[0])
                else:
                    wc = float(wc)
                if isinstance(ww, (list, tuple)):
                    ww = float(ww[0])
                else:
                    ww = float(ww)
                if ww > 0:
                    low = wc - ww / 2.0
                    high = wc + ww / 2.0
                    arr = np.clip(arr, low, high)
        except Exception:
            pass

    amin = np.nanmin(arr)
    amax = np.nanmax(arr)
    if not np.isfinite(amin) or not np.isfinite(amax) or amax <= amin:
        out = np.zeros(arr.shape, dtype=np.uint8)
    else:
        out = (arr - amin) / (amax - amin)
        out = (out * 255.0).clip(0, 255).astype(np.uint8)

    photometric = str(getattr(ds, "PhotometricInterpretation", "") or "").upper()
    if photometric == "MONOCHROME1":
        out = 255 - out

    return out


def save_png_u8(u8: np.ndarray, out_path: Path, mode: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(u8, mode=mode).save(str(out_path), optimize=True)


def find_dicom_dirs(root: Path, followlinks: bool = True):
    dicom_dirs = []
    for dirpath, _, _ in os.walk(root, followlinks=followlinks):
        p = Path(dirpath)
        if p.name.lower() == "dicom":
            dicom_dirs.append(p)
    return dicom_dirs


def collect_dcm_files(dicom_dir: Path):
    files = []
    for dirpath, _, filenames in os.walk(dicom_dir, followlinks=True):
        for fn in filenames:
            if fn.lower().endswith(".dcm"):
                files.append(Path(dirpath) / fn)
    return files


def out_path_for(dcm_path: Path, out_root: Path, rel_root: Path) -> Path:
    rel = dcm_path.relative_to(rel_root)
    return (out_root / rel).with_suffix(".png")


def get_frame_u8(ds, frame_index: int = 0):
    if "PixelData" not in ds:
        raise ValueError("No PixelData")

    arr = ds.pixel_array

    # 1) 单帧灰度: (H, W)
    if arr.ndim == 2:
        u8 = to_uint8(arr, ds)
        return u8, 1, "L"

    # 2) 单帧彩色: (H, W, 3/4)
    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        img = arr
        if img.dtype != np.uint8:
            img = img.astype(np.float32)
            mn, mx = np.nanmin(img), np.nanmax(img)
            if np.isfinite(mn) and np.isfinite(mx) and mx > mn:
                img = ((img - mn) / (mx - mn) * 255.0).clip(0, 255).astype(np.uint8)
            else:
                img = np.zeros_like(img, dtype=np.uint8)
        mode = "RGB" if img.shape[-1] == 3 else "RGBA"
        return img, 1, mode

    # 3) YBR_FULL_422 转换为 RGB（适配超声图像的色彩空间）
    if getattr(ds, "PhotometricInterpretation", "") == "YBR_FULL_422":
        if arr.ndim == 3 and arr.shape[-1] == 3:
            ybr = arr.astype(np.float32)
            rgb = np.empty_like(ybr)
            rgb[..., 0] = ybr[..., 0] + 1.402 * (ybr[..., 2] - 128)
            rgb[..., 1] = ybr[..., 0] - 0.344136 * (ybr[..., 1] - 128) - 0.714136 * (ybr[..., 2] - 128)
            rgb[..., 2] = ybr[..., 0] + 1.772 * (ybr[..., 1] - 128)
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
            return rgb, 1, "RGB"
        else:
            return arr, 1, "L"

    # 4) 多帧灰度: (F, H, W)
    if arr.ndim == 3:
        frame0 = arr[0]
        u8 = to_uint8(frame0, ds)
        return u8, arr.shape[0], "L"

    # 5) 多帧彩色: (F, H, W, 3/4)
    if arr.ndim == 4 and arr.shape[-1] in (3, 4):
        img = arr[0]
        if img.dtype != np.uint8:
            img = img.astype(np.float32)
            mn, mx = np.nanmin(img), np.nanmax(img)
            if np.isfinite(mn) and np.isfinite(mx) and mx > mn:
                img = ((img - mn) / (mx - mn) * 255.0).clip(0, 255).astype(np.uint8)
            else:
                img = np.zeros_like(img, dtype=np.uint8)
        mode = "RGB" if img.shape[-1] == 3 else "RGBA"
        return img, arr.shape[0], mode

    raise ValueError(f"Unsupported shape: {arr.shape}")


def convert_one_worker(dcm_path_str: str, out_path_str: str) -> str:
    """
    支持：灰度 / RGB / RGBA；支持多帧（取第0帧）
    """
    dcm_path = Path(dcm_path_str)
    out_path = Path(out_path_str)

    try:
        ds = pydicom.dcmread(str(dcm_path), force=True)
        if "PixelData" not in ds:
            return "no_pixel"

        try:
            arr = ds.pixel_array
        except Exception:
            return "decode_fail"

        print(f"Processing {dcm_path}: shape={arr.shape}, Photometric={ds.PhotometricInterpretation}")

        # 1) 单帧灰度: (H, W)
        if arr.ndim == 2:
            u8 = to_uint8(arr, ds)
            save_png_u8(u8, out_path, mode="L")
            return "ok"

        # 2) 单帧彩色: (H, W, 3/4)
        if arr.ndim == 3 and arr.shape[-1] in (3, 4):
            img = arr
            if img.dtype != np.uint8:
                img = img.astype(np.float32)
                mn, mx = np.nanmin(img), np.nanmax(img)
                if np.isfinite(mn) and np.isfinite(mx) and mx > mn:
                    img = ((img - mn) / (mx - mn) * 255.0).clip(0, 255).astype(np.uint8)
                else:
                    img = np.zeros_like(img, dtype=np.uint8)
            mode = "RGB" if img.shape[-1] == 3 else "RGBA"
            save_png_u8(img, out_path, mode=mode)
            return "ok"

        # 3) 多帧灰度: (F, H, W)
        if arr.ndim == 3:
            frame0 = arr[0]
            u8 = to_uint8(frame0, ds)
            save_png_u8(u8, out_path, mode="L")
            return "ok"

        # 4) 多帧彩色: (F, H, W, 3/4)
        if arr.ndim == 4 and arr.shape[-1] in (3, 4):
            img = arr[0]
            if img.dtype != np.uint8:
                img = img.astype(np.float32)
                mn, mx = np.nanmin(img), np.nanmax(img)
                if np.isfinite(mn) and np.isfinite(mx) and mx > mn:
                    img = ((img - mn) / (mx - mn) * 255.0).clip(0, 255).astype(np.uint8)
                else:
                    img = np.zeros_like(img, dtype=np.uint8)
            mode = "RGB" if img.shape[-1] == 3 else "RGBA"
            save_png_u8(img, out_path, mode=mode)
            return "ok"

        return "unsupported_dim"

    except Exception:
        return "error"


def dry_run_worker(dcm_path_str: str) -> dict:
    """
    只读/只统计：不写文件
    返回 dict 方便主进程聚合
    """
    p = Path(dcm_path_str)
    info = {
        "status": "ok",
        "tsuid": "",
        "photo": "",
        "shape": "",
        "dtype": "",
    }
    try:
        ds = pydicom.dcmread(str(p), force=True)

        # TransferSyntaxUID
        tsuid = ""
        try:
            tsuid = str(ds.file_meta.TransferSyntaxUID)
        except Exception:
            tsuid = ""
        info["tsuid"] = tsuid

        # PhotometricInterpretation
        info["photo"] = str(getattr(ds, "PhotometricInterpretation", "") or "")

        if "PixelData" not in ds:
            info["status"] = "no_pixel"
            return info

        try:
            arr = ds.pixel_array
        except Exception:
            info["status"] = "decode_fail"
            return info

        info["shape"] = str(getattr(arr, "shape", ""))
        info["dtype"] = str(getattr(arr, "dtype", ""))

        # 维度粗分类
        # ok: (H,W), (H,W,3/4), (F,H,W), (F,H,W,3/4)
        if arr.ndim == 2:
            return info
        if arr.ndim == 3 and arr.shape[-1] in (3, 4):
            return info
        if arr.ndim == 3:
            return info
        if arr.ndim == 4 and arr.shape[-1] in (3, 4):
            return info

        info["status"] = "unsupported_dim"
        return info

    except Exception:
        info["status"] = "error"
        return info

def dry_run_worker_tuple(task):
    # 这里 task 只应该是一个字符串，不是元组
    dcm_path_str = task[0]  # 取元组中的第一个元素作为路径
    return dry_run_worker(dcm_path_str)


def convert_one_worker_tuple(task):
    # 这里 task 只应该是一个字符串，不是元组
    dcm_path_str = task[0]  # 取元组中的第一个元素作为路径
    out_path_str = task[1]  # 取元组中的第二个元素作为输出路径
    return convert_one_worker(dcm_path_str, out_path_str)

def main():
    ap = argparse.ArgumentParser(description="Parallel DICOM -> PNG (Check validity).")
    ap.add_argument(
        "--root",
        default="/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid",
        help="By_Sickid 根目录"
    )
    ap.add_argument(
        "--out_root",
        default="/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_SickidDicom",
        help="统一输出目录"
    )
    ap.add_argument("--followlinks", action="store_true", help="Follow symlinks")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing PNGs")
    ap.add_argument("--dry_run", action="store_true", help="Only check files, do not output PNGs")
    ap.add_argument("--limit", type=int, default=0, help="Limit the number of files to process")
    ap.add_argument("--sample_every", type=int, default=0, help="Sample every Nth file")
    ap.add_argument("--workers", type=int, default=0, help="Number of workers (default: auto)")
    ap.add_argument("--chunksize", type=int, default=32, help="Task chunk size")

    args = ap.parse_args()
    root = Path(args.root)
    out_root = Path(args.out_root)

    if not root.exists():
        raise FileNotFoundError(f"root not found: {root}")

    dicom_dirs = find_dicom_dirs(root, followlinks=args.followlinks)
    all_dcms = []
    for ddir in dicom_dirs:
        all_dcms.extend(collect_dcm_files(ddir))

    if not all_dcms:
        print(f"No .dcm files found in {root}")
        return

    # Fix: generate tasks as tuples of (dcm_path, out_path)
    tasks = [(str(dcm), str(out_path_for(dcm, out_root, root))) for dcm in all_dcms]

    if args.dry_run:
        # Execute dry run and collect stats
        status_cnt = Counter()
        ts_cnt = Counter()
        photo_cnt = Counter()
        shape_cnt = Counter()

        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            it = ex.map(dry_run_worker_tuple, tasks, chunksize=args.chunksize)
            for info in tqdm(it, total=len(tasks), desc="Dry Run", unit="file"):
                status_cnt[info["status"]] += 1
                ts_cnt[info["tsuid"]] += 1
                photo_cnt[info["photo"]] += 1
                shape_cnt[info["shape"]] += 1

        print("Dry Run Summary")
        print("Status:", status_cnt)
        print("TransferSyntaxUIDs:", ts_cnt)
        print("PhotometricInterpretations:", photo_cnt)
        print("Shape counts:", shape_cnt)
        return

    print("Processing...")
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for result in tqdm(ex.map(convert_one_worker_tuple, tasks, chunksize=args.chunksize), total=len(tasks)):
            pass

    print("Processing completed.")


if __name__ == "__main__":
    main()



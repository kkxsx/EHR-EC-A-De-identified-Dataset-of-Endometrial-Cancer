#!/usr/bin/env python3
"""
Prepare a public image dataset from By_Sickid:
1) Normalize pathology directories to <patient>/pathology/
2) Flatten sensitive pathology subdirectories (keep only filename)
3) Convert DICOM files to JPG under <patient>/dicom/
4) Copy real files (no symlinks)

Recommended workflow:
- Dry run first to estimate required space
- Execute after confirming free space is enough
"""

import argparse
import concurrent.futures
import csv
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}
DICOM_EXTS = {".dcm", ".dicom"}


class Operation:
    __slots__ = ("kind", "src", "dst", "src_bytes")

    def __init__(self, kind: str, src: Path, dst: Path, src_bytes: int):
        self.kind = kind  # copy_pathology | convert_dicom
        self.src = src
        self.dst = dst
        self.src_bytes = src_bytes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare public dataset from By_Sickid (copy pathology + convert dicom to jpg)."
    )
    parser.add_argument(
        "--src-root",
        type=Path,
        default=Path(
            "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid"
        ),
        help="Source By_Sickid directory.",
    )
    parser.add_argument(
        "--dst-root",
        type=Path,
        default=None,
        help="Target public dataset directory. Default: <src parent>/By_Sickid_public",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually run copy/conversion. Without this flag, only dry-run statistics are produced.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing destination files. Default: skip existing files.",
    )
    parser.add_argument(
        "--ignore-space-check",
        action="store_true",
        help="Run even if estimated required size exceeds free space.",
    )
    parser.add_argument(
        "--dicom-jpg-ratio",
        type=float,
        default=0.35,
        help="Estimated dicom->jpg size ratio used for expected-space estimate (default: 0.35).",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=int,
        default=95,
        help="JPEG quality for converted dicom images (default: 95).",
    )
    parser.add_argument(
        "--report-csv",
        type=Path,
        default=None,
        help="CSV report path. Default: <dst_root>/prepare_report.csv",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1000,
        help="Print progress every N files during execute (default: 1000).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers for execute phase (default: 1).",
    )
    parser.add_argument(
        "--no-tqdm",
        action="store_true",
        help="Disable tqdm progress bar and use interval logging only.",
    )
    parser.add_argument(
        "--allow-dup-suffix",
        action="store_true",
        help="Allow *_dupN suffix on name collision (default: disabled, keep first and skip collisions).",
    )
    return parser.parse_args()


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def nearest_existing_parent(path: Path) -> Path:
    cur = path
    while not cur.exists():
        if cur.parent == cur:
            break
        cur = cur.parent
    return cur


def unique_destination(dst: Path, used: Set[Path], allow_dup_suffix: bool) -> Path:
    candidate = dst
    # IMPORTANT: do not inspect existing files on disk here.
    # We keep deterministic mapping across reruns so resume can skip
    # already-produced files instead of generating new names.
    if candidate not in used:
        used.add(candidate)
        return candidate

    if not allow_dup_suffix:
        return None

    stem = dst.stem
    suffix = dst.suffix
    i = 1
    while True:
        candidate = dst.with_name(f"{stem}_dup{i}{suffix}")
        if candidate not in used:
            used.add(candidate)
            return candidate
        i += 1


def iter_patient_dirs(src_root: Path) -> Iterable[Path]:
    for p in sorted(src_root.iterdir()):
        if p.is_dir():
            yield p


def iter_files_followlinks(root: Path) -> Iterable[Path]:
    # Use os.walk with followlinks=True so symlinked subdirectories are traversed.
    # Guard against symlink loops by tracking visited real paths.
    visited = set()
    for dirpath, _, filenames in os.walk(str(root), followlinks=True):
        real_dir = os.path.realpath(dirpath)
        if real_dir in visited:
            continue
        visited.add(real_dir)

        base = Path(dirpath)
        for name in filenames:
            p = base / name
            if p.is_file():
                yield p


def collect_operations(
    src_root: Path, dst_root: Path, allow_dup_suffix: bool
) -> Tuple[List[Operation], Dict[str, int]]:
    operations: List[Operation] = []
    stats: Dict[str, int] = {
        "patients": 0,
        "pathology_files": 0,
        "dicom_files": 0,
        "ignored_non_image_pathology": 0,
        "pathology_name_collisions_skipped": 0,
        "dicom_name_collisions_skipped": 0,
    }

    used_destinations: Set[Path] = set()

    for patient_dir in iter_patient_dirs(src_root):
        stats["patients"] += 1
        patient_id = patient_dir.name
        dst_patient = dst_root / patient_id

        # 1) pathology* directories -> <patient>/pathology/<filename>
        pathology_dirs = [
            d
            for d in sorted(patient_dir.iterdir())
            if d.is_dir() and d.name.lower().startswith("pathology")
        ]

        for pathology_dir in pathology_dirs:
            for f in sorted(iter_files_followlinks(pathology_dir)):
                if f.suffix.lower() not in IMAGE_EXTS:
                    stats["ignored_non_image_pathology"] += 1
                    continue

                dst = dst_patient / "pathology" / f.name
                dst = unique_destination(
                    dst, used_destinations, allow_dup_suffix=allow_dup_suffix
                )
                if dst is None:
                    stats["pathology_name_collisions_skipped"] += 1
                    continue
                operations.append(
                    Operation(
                        kind="copy_pathology",
                        src=f,
                        dst=dst,
                        src_bytes=f.stat().st_size,
                    )
                )
                stats["pathology_files"] += 1

        # 2) dicom* directories -> <patient>/dicom/<stem>.jpg
        dicom_dirs = [
            d
            for d in sorted(patient_dir.iterdir())
            if d.is_dir() and d.name.lower().startswith("dicom")
        ]
        for dicom_dir in dicom_dirs:
            for f in sorted(iter_files_followlinks(dicom_dir)):
                if f.suffix.lower() not in DICOM_EXTS:
                    continue

                dst = dst_patient / "dicom" / f"{f.stem}.jpg"
                dst = unique_destination(
                    dst, used_destinations, allow_dup_suffix=allow_dup_suffix
                )
                if dst is None:
                    stats["dicom_name_collisions_skipped"] += 1
                    continue
                operations.append(
                    Operation(
                        kind="convert_dicom",
                        src=f,
                        dst=dst,
                        src_bytes=f.stat().st_size,
                    )
                )
                stats["dicom_files"] += 1

    return operations, stats


def estimate_space(
    operations: List[Operation], dst_root: Path, dicom_jpg_ratio: float
) -> Dict[str, int]:
    pathology_src = sum(op.src_bytes for op in operations if op.kind == "copy_pathology")
    dicom_src = sum(op.src_bytes for op in operations if op.kind == "convert_dicom")

    # Expected required size (practical estimate):
    expected_need = pathology_src + int(dicom_src * dicom_jpg_ratio)
    # Conservative upper bound: assume converted JPG ~= source DICOM size.
    conservative_need = pathology_src + dicom_src

    usage_base = nearest_existing_parent(dst_root)
    free_bytes = shutil.disk_usage(usage_base).free

    return {
        "pathology_src": pathology_src,
        "dicom_src": dicom_src,
        "expected_need": expected_need,
        "conservative_need": conservative_need,
        "free_bytes": free_bytes,
    }


def normalize_to_uint8(arr):
    import numpy as np

    arr = arr.astype(np.float32, copy=False)
    finite_mask = np.isfinite(arr)
    if not finite_mask.any():
        return np.zeros(arr.shape, dtype=np.uint8)

    min_v = float(arr[finite_mask].min())
    max_v = float(arr[finite_mask].max())
    if max_v <= min_v:
        return np.zeros(arr.shape, dtype=np.uint8)

    arr = (arr - min_v) / (max_v - min_v)
    arr = (arr * 255.0).clip(0, 255)
    return arr.astype(np.uint8)


def _dicom_to_jpg_via_pydicom(src: Path, dst: Path, jpeg_quality: int) -> None:
    import numpy as np
    import pydicom
    from pydicom.pixel_data_handlers.util import apply_modality_lut
    from PIL import Image

    ds = pydicom.dcmread(str(src), force=True)
    arr = ds.pixel_array

    # Apply rescale slope/intercept if present.
    try:
        arr = apply_modality_lut(arr, ds)
    except Exception:
        pass

    arr = np.asarray(arr)

    # Multi-frame color: (frames, H, W, 3/4) -> take first frame.
    if arr.ndim >= 4 and arr.shape[-1] in (3, 4):
        arr = arr[0]

    if arr.ndim == 3 and arr.shape[-1] in (3, 4):
        # RGB / RGBA-like image
        if arr.shape[-1] == 4:
            arr = arr[..., :3]
        arr_u8 = normalize_to_uint8(arr)
        image = Image.fromarray(arr_u8, mode="RGB")
    else:
        # Force grayscale 2D for single-channel save.
        while arr.ndim > 2:
            arr = arr[0]
        if arr.ndim == 1:
            arr = np.expand_dims(arr, axis=0)

        photometric = str(ds.get("PhotometricInterpretation", "")).upper()
        if photometric == "MONOCHROME1":
            arr = arr.max() - arr

        arr_u8 = normalize_to_uint8(arr)
        image = Image.fromarray(arr_u8, mode="L")

    dst.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(dst), format="JPEG", quality=jpeg_quality)


def _looks_like_missing_decoder_error(err: Exception) -> bool:
    text = repr(err)
    return (
        "Unable to decompress" in text
        or "all plugins are missing dependencies" in text
        or "JPEG Lossless" in text
    )


def _decompress_dicom_with_gdcmconv(src: Path) -> Path:
    gdcmconv_bin = shutil.which("gdcmconv")
    if not gdcmconv_bin:
        local_bin = Path.home() / ".local" / "bin" / "gdcmconv"
        if local_bin.exists():
            gdcmconv_bin = str(local_bin)
    if not gdcmconv_bin:
        raise RuntimeError("gdcmconv not found in PATH")

    fd, temp_path = tempfile.mkstemp(suffix=".dcm", prefix="gdcm_raw_")
    os.close(fd)
    temp_dcm = Path(temp_path)

    cmd = [gdcmconv_bin, "--raw", str(src), str(temp_dcm)]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        stderr_text = result.stderr.decode("utf-8", errors="ignore")
        try:
            if temp_dcm.exists():
                temp_dcm.unlink()
        except Exception:
            pass
        raise RuntimeError("gdcmconv failed: {}".format(stderr_text.strip()))

    return temp_dcm


def dicom_to_jpg(src: Path, dst: Path, jpeg_quality: int) -> None:
    # Primary path: pydicom direct decode.
    try:
        _dicom_to_jpg_via_pydicom(src, dst, jpeg_quality)
        return
    except Exception as primary_err:
        if not _looks_like_missing_decoder_error(primary_err):
            raise

    # Fallback path: decompress with gdcmconv, then decode again.
    temp_dcm = None
    try:
        temp_dcm = _decompress_dicom_with_gdcmconv(src)
        _dicom_to_jpg_via_pydicom(temp_dcm, dst, jpeg_quality)
    finally:
        if temp_dcm is not None:
            try:
                if temp_dcm.exists():
                    temp_dcm.unlink()
            except Exception:
                pass


def make_temp_destination(dst: Path) -> Path:
    token = uuid.uuid4().hex
    return dst.with_name(".tmp_{}_{}".format(dst.name, token))


def process_single_operation(op: Operation, overwrite: bool, jpeg_quality: int) -> Tuple[str, str]:
    if op.dst.exists() and not overwrite:
        try:
            if op.dst.stat().st_size > 0:
                return "skip", "exists"
        except Exception:
            # If metadata read fails, fall through and rebuild file.
            pass

    op.dst.parent.mkdir(parents=True, exist_ok=True)
    temp_dst = make_temp_destination(op.dst)

    try:
        if op.kind == "copy_pathology":
            shutil.copy2(op.src, temp_dst, follow_symlinks=True)
        elif op.kind == "convert_dicom":
            dicom_to_jpg(op.src, temp_dst, jpeg_quality=jpeg_quality)
        else:
            raise ValueError("Unknown operation kind: {}".format(op.kind))

        os.replace(str(temp_dst), str(op.dst))
        return "ok", ""
    except Exception as e:
        try:
            if temp_dst.exists():
                temp_dst.unlink()
        except Exception:
            pass
        return "fail", repr(e)


def create_tqdm_progress(total: int, disable_tqdm: bool):
    if disable_tqdm:
        return None

    try:
        from tqdm import tqdm
    except Exception:
        return None

    return tqdm(
        total=total,
        unit="file",
        desc="processing",
        dynamic_ncols=True,
        smoothing=0.1,
    )


def execute_operations(
    operations: List[Operation],
    overwrite: bool,
    jpeg_quality: int,
    progress_every: int,
    workers: int,
    disable_tqdm: bool,
    report_csv: Path,
) -> Dict[str, int]:
    results = {"ok": 0, "skip": 0, "fail": 0, "decode_plugin_fail": 0}

    report_csv.parent.mkdir(parents=True, exist_ok=True)
    with report_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["kind", "src", "dst", "status", "error"])

        total = len(operations)
        done = 0
        pbar = create_tqdm_progress(total=total, disable_tqdm=disable_tqdm)

        try:
            if workers <= 1:
                for op in operations:
                    status, err = process_single_operation(
                        op=op,
                        overwrite=overwrite,
                        jpeg_quality=jpeg_quality,
                    )
                    results[status] += 1
                    if status == "fail" and "Unable to decompress" in err:
                        results["decode_plugin_fail"] += 1

                    writer.writerow([op.kind, str(op.src), str(op.dst), status, err])
                    done += 1
                    if pbar is not None:
                        pbar.update(1)

                    if pbar is None and progress_every > 0 and (
                        done % progress_every == 0 or done == total
                    ):
                        print(
                            "[progress] {}/{} | ok={} skip={} fail={}".format(
                                done, total, results["ok"], results["skip"], results["fail"]
                            )
                        )
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                    future_to_op = {
                        executor.submit(
                            process_single_operation, op, overwrite, jpeg_quality
                        ): op
                        for op in operations
                    }
                    for future in concurrent.futures.as_completed(future_to_op):
                        op = future_to_op[future]
                        try:
                            status, err = future.result()
                        except Exception as e:
                            status, err = "fail", repr(e)

                        results[status] += 1
                        if status == "fail" and "Unable to decompress" in err:
                            results["decode_plugin_fail"] += 1

                        writer.writerow([op.kind, str(op.src), str(op.dst), status, err])
                        done += 1
                        if pbar is not None:
                            pbar.update(1)

                        if pbar is None and progress_every > 0 and (
                            done % progress_every == 0 or done == total
                        ):
                            print(
                                "[progress] {}/{} | ok={} skip={} fail={}".format(
                                    done, total, results["ok"], results["skip"], results["fail"]
                                )
                            )
        finally:
            if pbar is not None:
                pbar.close()

    return results


def check_conversion_dependencies(operations: List[Operation]) -> None:
    need_dicom = any(op.kind == "convert_dicom" for op in operations)
    if not need_dicom:
        return

    missing = []
    for mod in ("numpy", "pydicom", "PIL"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)

    if missing:
        mods = ", ".join(missing)
        raise RuntimeError(
            f"Missing Python dependencies: {mods}. "
            "Please install before --execute, e.g. `pip install numpy pydicom pillow`."
        )


def main() -> int:
    args = parse_args()
    src_root = args.src_root
    dst_root = args.dst_root if args.dst_root else (src_root.parent / "By_Sickid_public")
    report_csv = args.report_csv if args.report_csv else (dst_root / "prepare_report.csv")

    print(f"[info] src_root: {src_root}")
    print(f"[info] dst_root: {dst_root}")
    print(f"[info] mode: {'EXECUTE' if args.execute else 'DRY-RUN'}")

    if not src_root.exists():
        print(f"[error] Source root not found: {src_root}")
        return 2

    print("[info] scanning files...")
    operations, stats = collect_operations(
        src_root, dst_root, allow_dup_suffix=args.allow_dup_suffix
    )
    est = estimate_space(operations, dst_root, dicom_jpg_ratio=args.dicom_jpg_ratio)

    print("\n=== Scan Summary ===")
    print(f"patients: {stats['patients']}")
    print(f"pathology image files: {stats['pathology_files']}")
    print(f"dicom files: {stats['dicom_files']}")
    print(f"ignored non-image files under pathology*: {stats['ignored_non_image_pathology']}")
    print(
        f"pathology name collisions skipped: {stats['pathology_name_collisions_skipped']}"
    )
    print(f"dicom name collisions skipped: {stats['dicom_name_collisions_skipped']}")
    print(f"total operations: {len(operations)}")

    print("\n=== Space Estimate ===")
    print(f"pathology source bytes: {human_size(est['pathology_src'])}")
    print(f"dicom source bytes: {human_size(est['dicom_src'])}")
    print(
        f"expected need (using dicom_jpg_ratio={args.dicom_jpg_ratio}): "
        f"{human_size(est['expected_need'])}"
    )
    print(f"conservative need (upper bound): {human_size(est['conservative_need'])}")
    print(f"free bytes on target filesystem: {human_size(est['free_bytes'])}")

    if not args.execute:
        print("\n[dry-run] No files were written. Re-run with --execute to process files.")
        return 0

    # Space safety gate with 10% margin on expected estimate.
    min_recommended = int(est["expected_need"] * 1.10)
    if not args.ignore_space_check and est["free_bytes"] < min_recommended:
        print(
            "\n[error] Free space is below recommended threshold "
            f"(need >= {human_size(min_recommended)}, have {human_size(est['free_bytes'])})."
        )
        print("Use --ignore-space-check only if you accept the risk.")
        return 3

    try:
        check_conversion_dependencies(operations)
    except RuntimeError as e:
        print(f"[error] {e}")
        return 4

    print("\n[info] executing operations...")
    if not args.no_tqdm:
        try:
            import tqdm  # noqa: F401
        except Exception:
            print("[warn] tqdm is not installed; falling back to interval logging.")
            print("       install with: python3 -m pip install --user tqdm")

    results = execute_operations(
        operations=operations,
        overwrite=args.overwrite,
        jpeg_quality=args.jpeg_quality,
        progress_every=args.progress_every,
        workers=max(1, int(args.workers)),
        disable_tqdm=args.no_tqdm,
        report_csv=report_csv,
    )

    print("\n=== Execute Summary ===")
    print(f"ok: {results['ok']}")
    print(f"skip: {results['skip']}")
    print(f"fail: {results['fail']}")
    if results["decode_plugin_fail"] > 0:
        print(f"fail_due_to_missing_dicom_decoder_plugins: {results['decode_plugin_fail']}")
        print(
            "hint: install DICOM decoder plugins "
            "(e.g. `pip install pylibjpeg pylibjpeg-libjpeg` or `python-gdcm`) and rerun."
        )
    print(f"report: {report_csv}")

    if results["fail"] > 0:
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())

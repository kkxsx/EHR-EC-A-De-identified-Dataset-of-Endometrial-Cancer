#!/usr/bin/env python3
"""
Web interactive in-place crop tool for By_Sickid_public DICOM JPG outputs.

Features:
- Browser-based ROI drag crop (no desktop GUI required).
- Show source DICOM metadata (institution, rows/cols, NumberOfFrames).
- Rule reuse:
  1) patient-level default crop ratio
  2) institution+resolution-level default crop ratio
- Explicit confirmation before destructive operations (done in frontend JS).
- In-place write only after confirmed save.
- Rollback support via backup history (undo last / rollback all).
"""

import argparse
import json
import os
import shutil
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse


IMAGE_EXTS = {".jpg", ".jpeg"}
DICOM_EXTS = {".dcm", ".dicom"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Web interactive in-place DICOM JPG crop tool.")
    parser.add_argument(
        "--src-root",
        type=Path,
        default=Path("/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid"),
        help="Original By_Sickid root (for DICOM metadata lookup).",
    )
    parser.add_argument(
        "--public-root",
        type=Path,
        default=Path(
            "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid_public"
        ),
        help="By_Sickid_public root (JPG files are modified in-place here).",
    )
    parser.add_argument(
        "--state-json",
        type=Path,
        default=None,
        help="State file path. Default: <public_root>/.crop_session_state.json",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Backup directory for rollback. Default: <public_root>/.crop_backups",
    )
    parser.add_argument(
        "--review-all",
        action="store_true",
        help="Review all images even if already saved in session state.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=0,
        help="Start index in sorted public JPG list (default: 0).",
    )
    parser.add_argument(
        "--rollback-all",
        action="store_true",
        help="Rollback all saved edits recorded in state history, then exit.",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Web server host.")
    parser.add_argument("--port", type=int, default=8787, help="Web server port.")
    return parser.parse_args()


def iter_files_followlinks(root: Path) -> Iterable[Path]:
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


def rel_str(path: Path, root: Path) -> str:
    return str(path.relative_to(root).as_posix())


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def roi_to_ratio(roi: Tuple[int, int, int, int], width: int, height: int) -> List[float]:
    x, y, w, h = roi
    x0 = float(x) / float(width)
    y0 = float(y) / float(height)
    x1 = float(x + w) / float(width)
    y1 = float(y + h) / float(height)
    return [x0, y0, x1, y1]


def ratio_to_roi(ratio: List[float], width: int, height: int) -> Tuple[int, int, int, int]:
    x0 = clamp(int(round(ratio[0] * width)), 0, width - 1)
    y0 = clamp(int(round(ratio[1] * height)), 0, height - 1)
    x1 = clamp(int(round(ratio[2] * width)), x0 + 1, width)
    y1 = clamp(int(round(ratio[3] * height)), y0 + 1, height)
    return x0, y0, x1 - x0, y1 - y0


class CropSession(object):
    def __init__(
        self,
        src_root: Path,
        public_root: Path,
        state_json: Path,
        backup_dir: Path,
        review_all: bool,
        start_index: int,
    ):
        self.src_root = src_root
        self.public_root = public_root
        self.state_json = state_json
        self.backup_dir = backup_dir
        self.review_all = review_all

        self.pydicom = None
        self.Image = None

        self.patient_dicom_cache: Dict[str, Dict[str, List[str]]] = {}
        self.state = self._load_state()

        self.files = self.list_public_jpgs()
        self.rel_to_index = {rel_str(p, self.public_root): i for i, p in enumerate(self.files)}
        self.current_idx = clamp(start_index, 0, len(self.files))
        self.current_idx = self._seek_next_pending(self.current_idx)

    def ensure_dependencies(self) -> None:
        try:
            import pydicom  # type: ignore
            from PIL import Image  # type: ignore
        except Exception as e:
            raise RuntimeError(
                "Missing dependency for web mode. Install: pydicom pillow"
            ) from e
        self.pydicom = pydicom
        self.Image = Image

    def _load_state(self) -> Dict:
        if self.state_json.exists():
            with self.state_json.open("r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}

        data.setdefault("version", 1)
        data.setdefault("patient_rules", {})
        data.setdefault("inst_res_rules", {})
        data.setdefault("applied", {})
        data.setdefault("history", [])
        return data

    def save_state(self) -> None:
        self.state_json.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_json.with_name(self.state_json.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        os.replace(str(tmp), str(self.state_json))

    def list_public_jpgs(self) -> List[Path]:
        files = []
        backup_root = self.backup_dir.resolve()
        for p in sorted(self.public_root.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            if p.parent.name != "dicom":
                continue

            # Never include backup snapshots as review targets.
            # We guard by both resolved path and rel-path segment checks to
            # catch nested ".crop_backups" paths from older buggy runs.
            try:
                if p.resolve().is_relative_to(backup_root):
                    continue
            except Exception:
                pass
            rel = p.relative_to(self.public_root)
            if ".crop_backups" in rel.parts:
                continue

            files.append(p)
        return files

    def _iter_with_progress(self, items: List[Path], desc: str) -> Iterable[Path]:
        total = len(items)
        if total <= 0:
            return []

        # Prefer tqdm in interactive terminals, fallback to plain text progress.
        try:
            from tqdm import tqdm  # type: ignore

            if sys.stdout.isatty():
                return tqdm(items, total=total, desc=desc, unit="img", dynamic_ncols=True)
        except Exception:
            pass

        def _plain_iter():
            started = time.time()
            print("[progress] {}: 0/{} (0.0%)".format(desc, total), flush=True)
            step = max(1, total // 100)
            for i, item in enumerate(items, 1):
                yield item
                if i == total or (i % step == 0):
                    elapsed = max(1e-6, time.time() - started)
                    rate = float(i) / elapsed
                    eta = float(total - i) / rate if rate > 0 else 0.0
                    print(
                        "[progress] {}: {}/{} ({:.1f}%) elapsed={:.1f}s eta={:.1f}s".format(
                            desc, i, total, 100.0 * i / float(total), elapsed, eta
                        ),
                        flush=True,
                    )

        return _plain_iter()

    def _is_unapplied(self, p: Path) -> bool:
        relp = rel_str(p, self.public_root)
        return relp not in self.state.get("applied", {})

    def _is_pending(self, p: Path) -> bool:
        return self.review_all or self._is_unapplied(p)

    def _seek_next_pending(self, idx: int) -> int:
        while idx < len(self.files):
            if self._is_pending(self.files[idx]):
                return idx
            idx += 1
        return idx

    def _seek_nearest_unprocessed_patient(
        self, anchor_idx: int, exclude_patient_id: Optional[str] = None
    ) -> int:
        """
        Find nearest index whose patient still has an unapplied image.
        Prefer not to stay on current patient after a batch operation.
        """
        n = len(self.files)
        if n == 0:
            return 0
        anchor_idx = clamp(anchor_idx, 0, n - 1)

        def _match(idx: int, allow_excluded: bool) -> bool:
            p = self.files[idx]
            if not self._is_unapplied(p):
                return False
            if (not allow_excluded) and exclude_patient_id is not None:
                if p.parent.parent.name == exclude_patient_id:
                    return False
            return True

        # First pass: nearest unapplied image, excluding current patient.
        for dist in range(0, n):
            right = anchor_idx + dist
            if right < n and _match(right, allow_excluded=False):
                return right
            if dist > 0:
                left = anchor_idx - dist
                if left >= 0 and _match(left, allow_excluded=False):
                    return left

        # Fallback: allow excluded patient (e.g., partial batch failures).
        for dist in range(0, n):
            right = anchor_idx + dist
            if right < n and _match(right, allow_excluded=True):
                return right
            if dist > 0:
                left = anchor_idx - dist
                if left >= 0 and _match(left, allow_excluded=True):
                    return left

        return n

    def _seek_first_unapplied(self) -> int:
        for idx, p in enumerate(self.files):
            if self._is_unapplied(p):
                return idx
        return len(self.files)

    def _seek_first_unapplied_without_rule(self) -> int:
        for idx, p in enumerate(self.files):
            if not self._is_unapplied(p):
                continue
            meta = self.get_meta(p)
            ratio, _ = self._suggested_ratio(meta["patient_id"], meta["inst_res_key"])
            if ratio is None:
                return idx
        return len(self.files)

    def has_current(self) -> bool:
        return self.current_idx < len(self.files)

    def current_file(self) -> Optional[Path]:
        if not self.has_current():
            return None
        return self.files[self.current_idx]

    def build_patient_dicom_index(self, patient_id: str) -> Dict[str, List[str]]:
        if patient_id in self.patient_dicom_cache:
            return self.patient_dicom_cache[patient_id]

        result: Dict[str, List[str]] = {}
        patient_dir = self.src_root / patient_id
        if not patient_dir.is_dir():
            self.patient_dicom_cache[patient_id] = result
            return result

        dicom_dirs = [
            d for d in sorted(patient_dir.iterdir()) if d.is_dir() and d.name.lower().startswith("dicom")
        ]
        for ddir in dicom_dirs:
            for p in iter_files_followlinks(ddir):
                if p.suffix.lower() not in DICOM_EXTS:
                    continue
                result.setdefault(p.stem, []).append(str(p))

        self.patient_dicom_cache[patient_id] = result
        return result

    def find_source_dicom(self, patient_id: str, stem: str) -> Optional[Path]:
        idx = self.build_patient_dicom_index(patient_id)
        arr = idx.get(stem, [])
        if not arr:
            return None
        return Path(sorted(arr)[0])

    def _image_size(self, public_jpg: Path) -> Tuple[int, int]:
        with self.Image.open(str(public_jpg)) as im:
            return int(im.width), int(im.height)

    def get_meta(self, public_jpg: Path) -> Dict[str, str]:
        patient_id = public_jpg.parent.parent.name
        stem = public_jpg.stem

        institution = "UNKNOWN"
        rows = 0
        cols = 0
        number_of_frames = "1"
        src_dicom = self.find_source_dicom(patient_id, stem)

        if src_dicom is not None:
            try:
                ds = self.pydicom.dcmread(str(src_dicom), stop_before_pixels=True, force=True)
                institution = str(ds.get("InstitutionName", "UNKNOWN")).strip() or "UNKNOWN"
                rows = int(ds.get("Rows", 0) or 0)
                cols = int(ds.get("Columns", 0) or 0)
                number_of_frames = str(ds.get("NumberOfFrames", 1))
            except Exception:
                pass

        if rows <= 0 or cols <= 0:
            w, h = self._image_size(public_jpg)
            cols, rows = w, h

        inst_res_key = "{}|{}x{}".format(institution, rows, cols)
        return {
            "patient_id": patient_id,
            "stem": stem,
            "institution": institution,
            "rows": str(rows),
            "cols": str(cols),
            "number_of_frames": number_of_frames,
            "inst_res_key": inst_res_key,
            "src_dicom": str(src_dicom) if src_dicom else "",
        }

    def _make_backup_path(self, target: Path) -> Path:
        rel = target.relative_to(self.public_root)
        stamp = str(int(time.time()))
        backup = self.backup_dir / stamp / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        return backup

    def _sanitize_roi(self, roi: List[int], width: int, height: int) -> Tuple[int, int, int, int]:
        if len(roi) != 4:
            raise RuntimeError("ROI must be [x,y,w,h]")
        x, y, w, h = [int(v) for v in roi]
        x = clamp(x, 0, width - 1)
        y = clamp(y, 0, height - 1)
        w = clamp(w, 1, width - x)
        h = clamp(h, 1, height - y)
        return x, y, w, h

    def _advance(self) -> None:
        self.current_idx += 1
        self.current_idx = self._seek_next_pending(self.current_idx)

    def _suggested_ratio(self, patient_id: str, inst_res_key: str) -> Tuple[Optional[List[float]], str]:
        if patient_id in self.state["patient_rules"]:
            return self.state["patient_rules"][patient_id], "patient-default"
        if inst_res_key in self.state["inst_res_rules"]:
            return self.state["inst_res_rules"][inst_res_key], "inst-res-default"
        return None, ""

    def _move(self, delta: int) -> None:
        if len(self.files) == 0:
            self.current_idx = 0
            return
        self.current_idx = clamp(self.current_idx + delta, 0, len(self.files) - 1)

    def current_payload(self) -> Dict:
        if not self.has_current():
            return {
                "done": True,
                "message": "No more images.",
                "total": len(self.files),
                "index": len(self.files),
            }

        jpg = self.current_file()
        relp = rel_str(jpg, self.public_root)
        applied_info = self.state.get("applied", {}).get(relp)
        is_applied = applied_info is not None
        meta = self.get_meta(jpg)
        w, h = self._image_size(jpg)

        ratio, source = self._suggested_ratio(meta["patient_id"], meta["inst_res_key"])
        suggested_roi = ratio_to_roi(ratio, w, h) if (ratio and (not is_applied)) else None
        patient_ratio = self.state["patient_rules"].get(meta["patient_id"])
        inst_ratio = self.state["inst_res_rules"].get(meta["inst_res_key"])
        patient_rule_roi = ratio_to_roi(patient_ratio, w, h) if patient_ratio else None
        inst_rule_roi = ratio_to_roi(inst_ratio, w, h) if inst_ratio else None

        return {
            "done": False,
            "index": self.current_idx,
            "total": len(self.files),
            "rel_path": relp,
            "image_url": "/api/image?rel=" + quote(relp),
            "width": w,
            "height": h,
            "meta": meta,
            "suggested_roi": suggested_roi,
            "patient_rule_roi": patient_rule_roi,
            "inst_rule_roi": inst_rule_roi,
            "rule_source": source,
            "is_applied": is_applied,
            "applied_rule_source": (applied_info or {}).get("rule_source", ""),
            "applied_count": len(self.state.get("applied", {})),
            "history_count": len(self.state.get("history", [])),
        }

    def _save_crop_to_file(
        self,
        target_jpg: Path,
        roi: Tuple[int, int, int, int],
        rule_source: str,
        patient_id: str,
        inst_res_key: str,
        set_patient_rule: bool,
        set_inst_rule: bool,
        persist_state: bool = True,
    ) -> None:
        with self.Image.open(str(target_jpg)) as im:
            width, height = im.width, im.height
            x, y, w, h = roi
            x = clamp(x, 0, width - 1)
            y = clamp(y, 0, height - 1)
            w = clamp(w, 1, width - x)
            h = clamp(h, 1, height - y)

            crop = im.crop((x, y, x + w, y + h))
            if crop.width <= 0 or crop.height <= 0:
                raise RuntimeError("Empty crop result")

            backup_path = self._make_backup_path(target_jpg)
            shutil.copy2(str(target_jpg), str(backup_path))

            tmp = target_jpg.with_name(".tmp_crop_{}".format(target_jpg.name))
            crop.convert("RGB").save(str(tmp), format="JPEG", quality=95)
            os.replace(str(tmp), str(target_jpg))

            relp = rel_str(target_jpg, self.public_root)
            ratio = roi_to_ratio((x, y, w, h), width, height)
            action = {
                "time": int(time.time()),
                "rel_path": relp,
                "backup_path": str(backup_path),
                "roi": [x, y, w, h],
                "ratio": ratio,
                "rule_source": rule_source,
                "patient_id": patient_id,
                "inst_res_key": inst_res_key,
                "undone": False,
            }
            self.state["history"].append(action)
            self.state["applied"][relp] = {
                "time": action["time"],
                "ratio": ratio,
                "rule_source": rule_source,
            }

            # same patient default should follow this crop
            self.state["patient_rules"][patient_id] = ratio
            if set_patient_rule:
                self.state["patient_rules"][patient_id] = ratio
            if set_inst_rule:
                self.state["inst_res_rules"][inst_res_key] = ratio

            if persist_state:
                self.save_state()

    def save_current_crop(self, roi: List[int], set_inst_rule: bool = False) -> Dict:
        if not self.has_current():
            raise RuntimeError("No current image")

        jpg = self.current_file()
        relp = rel_str(jpg, self.public_root)
        if not self._is_unapplied(jpg):
            return {
                "ok": True,
                "message": "Skipped already-cropped image: {}".format(relp),
                "current": self.current_payload(),
            }
        meta = self.get_meta(jpg)
        w, h = self._image_size(jpg)
        clean_roi = self._sanitize_roi(roi, w, h)

        self._save_crop_to_file(
            target_jpg=jpg,
            roi=clean_roi,
            rule_source="manual",
            patient_id=meta["patient_id"],
            inst_res_key=meta["inst_res_key"],
            set_patient_rule=True,
            set_inst_rule=set_inst_rule,
        )
        return {
            "ok": True,
            "message": "Saved in-place: {}".format(relp),
            "current": self.current_payload(),
        }

    def batch_apply_current(self, roi: List[int], scope: str, set_inst_rule: bool = False) -> Dict:
        if not self.has_current():
            raise RuntimeError("No current image")
        if scope not in ("patient", "inst_res"):
            raise RuntimeError("Unknown batch scope: {}".format(scope))

        cur = self.current_file()
        cur_relp = rel_str(cur, self.public_root)
        if not self._is_unapplied(cur):
            return {
                "ok": True,
                "message": "Skipped batch: current image already cropped ({})".format(cur_relp),
                "current": self.current_payload(),
            }
        cur_meta = self.get_meta(cur)
        cw, ch = self._image_size(cur)
        clean_roi = self._sanitize_roi(roi, cw, ch)
        base_ratio = roi_to_ratio(clean_roi, cw, ch)

        target_patient = cur_meta["patient_id"]
        target_inst_res = cur_meta["inst_res_key"]

        applied = 0
        skipped_applied = 0
        failed: List[str] = []

        print(
            "[info] batch_apply_current start: scope={}, total_candidates={}".format(
                scope, len(self.files)
            ),
            flush=True,
        )
        for p in self._iter_with_progress(self.files, "batch-{}".format(scope)):
            relp = rel_str(p, self.public_root)
            if relp in self.state.get("applied", {}):
                skipped_applied += 1
                continue

            if scope == "patient":
                if p.parent.parent.name != target_patient:
                    continue
                meta = self.get_meta(p)
            else:
                meta = self.get_meta(p)
                if meta["inst_res_key"] != target_inst_res:
                    continue

            try:
                w, h = self._image_size(p)
                roi_p = ratio_to_roi(base_ratio, w, h)
                self._save_crop_to_file(
                    target_jpg=p,
                    roi=roi_p,
                    rule_source="{}-batch".format(scope),
                    patient_id=meta["patient_id"],
                    inst_res_key=meta["inst_res_key"],
                    set_patient_rule=False,
                    set_inst_rule=False,
                    persist_state=False,
                )
                applied += 1
            except Exception as e:
                failed.append("{} => {}".format(relp, repr(e)))

        # Persist session defaults once per batch.
        self.state["patient_rules"][target_patient] = base_ratio
        if scope == "inst_res" or set_inst_rule:
            self.state["inst_res_rules"][target_inst_res] = base_ratio
        self.save_state()

        # Jump to nearest patient that still has unapplied images.
        self.current_idx = self._seek_nearest_unprocessed_patient(
            anchor_idx=self.current_idx,
            exclude_patient_id=target_patient,
        )

        msg = "Batch {} applied={}, skipped_already_applied={}, failed={}".format(
            scope, applied, skipped_applied, len(failed)
        )
        print("[info] {}".format(msg), flush=True)
        if failed:
            preview = "; ".join(failed[:3])
            msg = msg + " | sample_fail: " + preview
        if self.has_current():
            next_patient = self.current_file().parent.parent.name
            msg = msg + " | jumped_to_patient={}".format(next_patient)
        else:
            msg = msg + " | no_unprocessed_patient_left"
        return {"ok": True, "message": msg, "current": self.current_payload()}

    def apply_default_current(self, set_inst_rule: bool = False) -> Dict:
        if not self.has_current():
            raise RuntimeError("No current image")

        jpg = self.current_file()
        relp = rel_str(jpg, self.public_root)
        if not self._is_unapplied(jpg):
            return {
                "ok": True,
                "message": "Skipped already-cropped image: {}".format(relp),
                "current": self.current_payload(),
            }
        meta = self.get_meta(jpg)
        w, h = self._image_size(jpg)

        ratio, source = self._suggested_ratio(meta["patient_id"], meta["inst_res_key"])
        if ratio is None:
            raise RuntimeError("No default rule available for current image")
        roi = ratio_to_roi(ratio, w, h)

        self._save_crop_to_file(
            target_jpg=jpg,
            roi=roi,
            rule_source=source,
            patient_id=meta["patient_id"],
            inst_res_key=meta["inst_res_key"],
            set_patient_rule=True,
            set_inst_rule=set_inst_rule,
        )
        return {
            "ok": True,
            "message": "Applied {} and saved: {}".format(source, relp),
            "current": self.current_payload(),
        }

    def auto_apply_existing_rules(self) -> Dict:
        """
        Auto-crop every unapplied image that already has a suggested rule
        (patient-rule first, then institution+resolution rule).
        Leave no-rule cases for manual review.
        """
        applied = 0
        skipped_applied = 0
        no_rule = 0
        failed: List[str] = []

        print(
            "[info] auto_apply_existing_rules start: total_candidates={}".format(len(self.files)),
            flush=True,
        )
        for p in self._iter_with_progress(self.files, "auto-apply-rules"):
            relp = rel_str(p, self.public_root)
            if not self._is_unapplied(p):
                skipped_applied += 1
                continue

            meta = self.get_meta(p)
            ratio, source = self._suggested_ratio(meta["patient_id"], meta["inst_res_key"])
            if ratio is None:
                no_rule += 1
                continue

            try:
                w, h = self._image_size(p)
                roi_p = ratio_to_roi(ratio, w, h)
                self._save_crop_to_file(
                    target_jpg=p,
                    roi=roi_p,
                    rule_source="{}-auto".format(source),
                    patient_id=meta["patient_id"],
                    inst_res_key=meta["inst_res_key"],
                    set_patient_rule=False,
                    set_inst_rule=False,
                    persist_state=False,
                )
                applied += 1
            except Exception as e:
                failed.append("{} => {}".format(relp, repr(e)))

        self.save_state()

        # Prefer unresolved items with no rule, otherwise any unapplied remaining item.
        idx_no_rule = self._seek_first_unapplied_without_rule()
        idx_any = self._seek_first_unapplied()
        if idx_no_rule < len(self.files):
            self.current_idx = idx_no_rule
            jump_reason = "manual_needed_no_rule"
        elif idx_any < len(self.files):
            self.current_idx = idx_any
            jump_reason = "manual_needed_remaining"
        else:
            self.current_idx = len(self.files)
            jump_reason = "all_done"

        msg = "Auto-apply existing rules: applied={}, no_rule={}, skipped_already_applied={}, failed={}".format(
            applied, no_rule, skipped_applied, len(failed)
        )
        print("[info] {}".format(msg), flush=True)
        if failed:
            msg = msg + " | sample_fail: " + "; ".join(failed[:3])
        if self.has_current():
            next_patient = self.current_file().parent.parent.name
            msg = msg + " | jumped_to_patient={} | reason={}".format(next_patient, jump_reason)
        else:
            msg = msg + " | no_unapplied_image_left"
        return {"ok": True, "message": msg, "current": self.current_payload()}

    def set_rule_current(self, roi: List[int], scope: str) -> Dict:
        if not self.has_current():
            raise RuntimeError("No current image")

        jpg = self.current_file()
        meta = self.get_meta(jpg)
        w, h = self._image_size(jpg)
        clean_roi = self._sanitize_roi(roi, w, h)
        ratio = roi_to_ratio(clean_roi, w, h)

        if scope == "patient":
            self.state["patient_rules"][meta["patient_id"]] = ratio
            msg = "Saved patient rule for {}".format(meta["patient_id"])
        elif scope == "inst_res":
            self.state["inst_res_rules"][meta["inst_res_key"]] = ratio
            msg = "Saved institution+resolution rule for {}".format(meta["inst_res_key"])
        else:
            raise RuntimeError("Unknown rule scope: {}".format(scope))

        self.save_state()
        return {"ok": True, "message": msg, "current": self.current_payload()}

    def skip_current(self) -> Dict:
        if self.has_current():
            cur = self.current_file()
            patient_id = cur.parent.parent.name
            start_idx = self.current_idx
            while self.current_idx < len(self.files):
                p = self.files[self.current_idx]
                if p.parent.parent.name != patient_id:
                    break
                self.current_idx += 1
            self.current_idx = self._seek_next_pending(self.current_idx)
            skipped = self.current_idx - start_idx
            return {
                "ok": True,
                "message": "Skipped patient {} ({} image slot(s))".format(patient_id, skipped),
                "current": self.current_payload(),
            }
        return {"ok": True, "message": "No current image", "current": self.current_payload()}

    def next_image(self) -> Dict:
        if not self.has_current():
            return {"ok": True, "message": "No current image", "current": self.current_payload()}
        self._move(+1)
        return {"ok": True, "message": "Moved to next image", "current": self.current_payload()}

    def prev_image(self) -> Dict:
        if not self.has_current():
            return {"ok": True, "message": "No current image", "current": self.current_payload()}
        self._move(-1)
        return {"ok": True, "message": "Moved to previous image", "current": self.current_payload()}

    def undo_last(self) -> Dict:
        hist = self.state.get("history", [])
        for i in range(len(hist) - 1, -1, -1):
            action = hist[i]
            if action.get("undone", False):
                continue

            relp = action["rel_path"]
            backup = Path(action["backup_path"])
            target = self.public_root / relp
            if not backup.exists():
                action["undone"] = True
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(backup), str(target))
            action["undone"] = True
            if relp in self.state["applied"]:
                del self.state["applied"][relp]
            self.save_state()

            if relp in self.rel_to_index:
                self.current_idx = self.rel_to_index[relp]
            return {
                "ok": True,
                "message": "Undo restored {}".format(relp),
                "current": self.current_payload(),
            }

        return {"ok": True, "message": "No action to undo", "current": self.current_payload()}

    def rollback_all(self) -> Dict:
        count = 0
        while True:
            res = self.undo_last()
            if "No action to undo" in res.get("message", ""):
                break
            count += 1
        return {
            "ok": True,
            "message": "Rollback-all restored {} action(s)".format(count),
            "current": self.current_payload(),
        }

    def read_image_bytes(self, rel_path: str) -> bytes:
        # Keep serving strictly inside public_root.
        rel_path = rel_path.lstrip("/")
        target = (self.public_root / rel_path).resolve()
        root = self.public_root.resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise RuntimeError("Invalid path")
        if not target.exists() or not target.is_file():
            raise RuntimeError("Image not found")
        if target.suffix.lower() not in IMAGE_EXTS:
            raise RuntimeError("Only JPG/JPEG can be served")
        return target.read_bytes()


HTML_PAGE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>DICOM Crop Web</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; padding: 0; background: #f7f7f7; }
    .top { padding: 12px 16px; background: #1f2937; color: #fff; }
    .wrap { display: flex; gap: 12px; padding: 12px; }
    .left { flex: 1; min-width: 720px; }
    .right { width: 420px; background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 12px; }
    .panel { background: #fff; border: 1px solid #ddd; border-radius: 8px; padding: 8px; }
    .img-wrap { position: relative; width: 100%; max-width: 1300px; border: 1px solid #ccc; background: #000; }
    #mainImage { width: 100%; height: auto; display: block; }
    #overlay { position: absolute; left: 0; top: 0; cursor: crosshair; }
    .meta { font-size: 13px; line-height: 1.5; white-space: pre-wrap; }
    .btns { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 10px; }
    button { padding: 10px; border: 1px solid #bbb; border-radius: 6px; background: #fff; cursor: pointer; }
    button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
    button.warn { background: #dc2626; color: #fff; border-color: #dc2626; }
    .status { margin-top: 10px; font-size: 13px; white-space: pre-wrap; background: #f9fafb; border: 1px solid #eee; padding: 8px; border-radius: 6px; }
    .small { font-size: 12px; color: #666; }
    .legend { margin-top: 8px; font-size: 12px; color: #444; }
    .legend span { display: inline-block; margin-right: 10px; }
    .dot { width: 10px; height: 10px; display: inline-block; margin-right: 4px; vertical-align: middle; }
    #previewCanvas { width: 100%; border: 1px solid #ddd; border-radius: 6px; background: #111; margin-top: 6px; }
    .keys { margin-top: 8px; font-size: 12px; line-height: 1.5; color: #333; background: #f5f5f5; border: 1px solid #e5e5e5; border-radius: 6px; padding: 8px; }
  </style>
</head>
<body>
  <div class="top"><b>DICOM Crop Web</b> - in-place editing with confirmation & rollback</div>
  <div class="wrap">
    <div class="left panel">
      <div class="img-wrap" id="imgWrap">
        <img id="mainImage" alt="current image" />
        <canvas id="overlay"></canvas>
      </div>
      <div class="small" style="margin-top:6px;">Drag on image to select keep-area crop ROI.</div>
      <div class="legend">
        <span><i class=\"dot\" style=\"background:#00ff99\"></i>Patient Rule</span>
        <span><i class=\"dot\" style=\"background:#ffcc00\"></i>Inst+Res Rule</span>
        <span><i class=\"dot\" style=\"background:#00ffff\"></i>Current Selection</span>
      </div>
    </div>
    <div class="right">
      <div id="meta" class="meta">Loading...</div>
      <div class="btns">
        <button id="btnPrev">Previous</button>
        <button id="btnNext">Next</button>
        <button id="btnUsePatient">Use Patient Rule</button>
        <button id="btnUseInst">Use InstRes Rule</button>
        <button class="primary" id="btnSave">Save Crop</button>
        <button class="primary" id="btnBatchPatient">Apply To Same Patient</button>
        <button id="btnBatchInst">Batch Crop Same Inst+Res</button>
        <button id="btnSaveInst">Save Crop + InstRes Rule</button>
        <button id="btnApplyDefault">Apply Default Rule + Save</button>
        <button class="primary" id="btnAutoRules">Auto-Apply Existing Rules</button>
        <button id="btnSkip">Skip Patient</button>
        <button id="btnSetPatient">Set Patient Rule</button>
        <button id="btnSetInst">Set InstRes Rule</button>
        <button id="btnUndo">Undo Last</button>
        <button class="warn" id="btnRollback">Rollback All</button>
        <button class="warn" id="btnExit">Exit Server</button>
      </div>
      <div class="keys">
        Shortcuts:
        <br/>save: <b>S</b> | next: <b>→ / N</b> | prev: <b>← / P</b>
        <br/>use patient rule: <b>1</b> | use inst rule: <b>2</b>
        <br/>batch same patient: <b>B</b> | batch same inst+res: <b>G</b> | skip patient: <b>K</b>
        <br/>batch actions auto-jump to nearest unprocessed patient
        <br/>auto-apply existing rules: <b>A</b> (all unapplied images)
        <br/>apply suggested+save: <b>D</b> | undo: <b>U</b> | rollback all: <b>R</b> | exit server: <b>X</b>
      </div>
      <div class="small" style="margin-top:8px;">Crop preview (what will be saved):</div>
      <canvas id="previewCanvas" height="220"></canvas>
      <div id="previewInfo" class="small"></div>
      <div id="status" class="status">Ready.</div>
    </div>
  </div>

<script>
let current = null;
let selectedNat = null; // [x,y,w,h] in natural image coords
let drawing = false;
let startX = 0, startY = 0;
let overlay, img, ctx, previewCanvas, previewCtx;
const EDGE_SNAP_PX = 16;
const FULL_SPAN_THRESHOLD_X = 0.92;

function setStatus(msg) {
  document.getElementById('status').textContent = msg;
}

function displayRectFromNatural(nat) {
  if (!nat) return null;
  const sx = overlay.width / img.naturalWidth;
  const sy = overlay.height / img.naturalHeight;
  return {
    x: Math.round(nat[0] * sx),
    y: Math.round(nat[1] * sy),
    w: Math.round(nat[2] * sx),
    h: Math.round(nat[3] * sy),
  };
}

function naturalRectFromDisplay(rect) {
  const sx = img.naturalWidth / overlay.width;
  const sy = img.naturalHeight / overlay.height;
  let x = Math.round(rect.x * sx);
  let y = Math.round(rect.y * sy);
  let w = Math.round(rect.w * sx);
  let h = Math.round(rect.h * sy);

  x = Math.max(0, Math.min(img.naturalWidth - 1, x));
  y = Math.max(0, Math.min(img.naturalHeight - 1, y));
  w = Math.max(1, Math.min(img.naturalWidth - x, w));
  h = Math.max(1, Math.min(img.naturalHeight - y, h));
  return [x, y, w, h];
}

function clampDisplay(v, maxV) {
  return Math.max(0, Math.min(maxV, v));
}

function snapToEdges(v, maxV) {
  if (v <= EDGE_SNAP_PX) return 0;
  if ((maxV - v) <= EDGE_SNAP_PX) return maxV;
  return v;
}

function normalizeDisplayRectFromPoints(x1, y1, x2, y2) {
  const maxX = overlay.width;
  const maxY = overlay.height;
  const sx1 = snapToEdges(clampDisplay(x1, maxX), maxX);
  const sy1 = snapToEdges(clampDisplay(y1, maxY), maxY);
  const sx2 = snapToEdges(clampDisplay(x2, maxX), maxX);
  const sy2 = snapToEdges(clampDisplay(y2, maxY), maxY);

  let x = Math.min(sx1, sx2);
  let y = Math.min(sy1, sy2);
  let w = Math.max(1, Math.abs(sx2 - sx1));
  let h = Math.max(1, Math.abs(sy2 - sy1));

  // If width is already close to full span, snap to full width.
  // We intentionally DO NOT auto-expand to full height because
  // burn-in sensitive text is typically near top area and vertical
  // control should remain precise.
  if ((w / maxX) >= FULL_SPAN_THRESHOLD_X) {
    x = 0;
    w = maxX;
  }
  return {x, y, w, h};
}

function drawOverlay(tempDisplayRect=null) {
  ctx.clearRect(0, 0, overlay.width, overlay.height);

  function drawRect(rect, color, width=2, dash=[]) {
    if (!rect) return;
    ctx.save();
    ctx.setLineDash(dash);
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.strokeRect(rect.x, rect.y, rect.w, rect.h);
    ctx.restore();
  }

  const patientRect = current && current.patient_rule_roi ? displayRectFromNatural(current.patient_rule_roi) : null;
  const instRect = current && current.inst_rule_roi ? displayRectFromNatural(current.inst_rule_roi) : null;
  const selectRect = tempDisplayRect || displayRectFromNatural(selectedNat);

  drawRect(patientRect, '#00ff99', 2, [6, 4]);
  drawRect(instRect, '#ffcc00', 2, [2, 6]);
  drawRect(selectRect, '#00ffff', 3, []);
}

function updateMeta() {
  if (!current || current.done) {
    document.getElementById('meta').textContent = 'All done.';
    return;
  }
  const m = current.meta;
  const croppedStatus = current.is_applied
    ? `cropped: yes (${current.applied_rule_source || 'unknown'})`
    : 'cropped: no';
  const txt = [
    `index: ${current.index + 1} / ${current.total}`,
    `file: ${current.rel_path}`,
    `patient: ${m.patient_id}`,
    `institution: ${m.institution}`,
    `resolution (DICOM): ${m.rows}x${m.cols}`,
    `NumberOfFrames: ${m.number_of_frames}`,
    croppedStatus,
    `rule source: ${current.rule_source || 'none'}`,
    `patient rule roi: ${current.patient_rule_roi ? current.patient_rule_roi.join(',') : 'none'}`,
    `inst+res rule roi: ${current.inst_rule_roi ? current.inst_rule_roi.join(',') : 'none'}`,
    `suggested roi: ${current.suggested_roi ? current.suggested_roi.join(',') : 'none'}`,
    `applied count: ${current.applied_count}, history count: ${current.history_count}`
  ].join('\n');
  document.getElementById('meta').textContent = txt;
}

function updatePreview() {
  if (!previewCanvas || !previewCtx) return;
  if (!selectedNat || !img || !img.complete || !img.naturalWidth) {
    previewCanvas.width = 360;
    previewCanvas.height = 220;
    previewCtx.clearRect(0, 0, previewCanvas.width, previewCanvas.height);
    document.getElementById('previewInfo').textContent = 'No ROI selected.';
    return;
  }

  const x = selectedNat[0], y = selectedNat[1], w = selectedNat[2], h = selectedNat[3];
  const maxW = 380, maxH = 240;
  const scale = Math.min(maxW / w, maxH / h, 1.0);
  const outW = Math.max(1, Math.round(w * scale));
  const outH = Math.max(1, Math.round(h * scale));

  previewCanvas.width = outW;
  previewCanvas.height = outH;
  previewCtx.clearRect(0, 0, outW, outH);
  previewCtx.drawImage(img, x, y, w, h, 0, 0, outW, outH);
  document.getElementById('previewInfo').textContent =
    `Preview ROI: [${selectedNat.join(',')}], output≈${w}x${h}`;
}

function setSelectedFromRule(scope) {
  if (!current || current.done) return;
  if (current.is_applied) {
    setStatus('This image is already cropped. Rule selection disabled.');
    return;
  }
  if (scope === 'patient') {
    if (!current.patient_rule_roi) {
      setStatus('No patient rule for current image.');
      return;
    }
    selectedNat = current.patient_rule_roi.slice();
    setStatus('Loaded patient rule into current selection.');
  } else if (scope === 'inst') {
    if (!current.inst_rule_roi) {
      setStatus('No institution+resolution rule for current image.');
      return;
    }
    selectedNat = current.inst_rule_roi.slice();
    setStatus('Loaded inst+res rule into current selection.');
  } else if (scope === 'suggested') {
    if (!current.suggested_roi) {
      setStatus('No suggested rule for current image.');
      return;
    }
    selectedNat = current.suggested_roi.slice();
    setStatus('Loaded suggested rule into current selection.');
  }
  drawOverlay();
  updatePreview();
}

async function fetchJSON(url, options={}) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || data.message || 'Request failed');
  }
  return data;
}

async function loadCurrent() {
  try {
    const data = await fetchJSON('/api/current');
    current = data;
    updateMeta();

    if (current.done) {
      setCropActionEnabled(false);
      img.src = '';
      selectedNat = null;
      drawOverlay();
      updatePreview();
      setStatus(current.message || 'Done.');
      return;
    }

    img.onload = () => {
      overlay.width = img.clientWidth;
      overlay.height = img.clientHeight;
      overlay.style.width = img.clientWidth + 'px';
      overlay.style.height = img.clientHeight + 'px';
      if (current.is_applied) {
        selectedNat = null;
      } else if (current.suggested_roi) {
        selectedNat = current.suggested_roi.slice();
      } else {
        selectedNat = null;
      }
      drawOverlay();
      updatePreview();
    };
    img.src = current.image_url + '&t=' + Date.now();
    setCropActionEnabled(!current.is_applied);
    if (current.is_applied) {
      setStatus('Loaded current image (already cropped, edit disabled).');
    } else {
      setStatus('Loaded current image.');
    }
  } catch (e) {
    setStatus('loadCurrent error: ' + e.message);
  }
}

async function postAction(path, body, confirmMsg=null) {
  if (confirmMsg && !window.confirm(confirmMsg)) return;
  try {
    const data = await fetchJSON(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body || {})
    });
    setStatus(data.message || 'ok');
    if (data.current) {
      current = data.current;
    }
    await loadCurrent();
  } catch (e) {
    setStatus('action error: ' + e.message);
  }
}

function requireROI() {
  if (current && current.is_applied) {
    setStatus('This image is already cropped. Save/batch is disabled.');
    return false;
  }
  if (!selectedNat) {
    setStatus('No ROI selected. Drag on image first.');
    return false;
  }
  return true;
}

function clickBtn(id) {
  const btn = document.getElementById(id);
  if (btn) btn.click();
}

function setCropActionEnabled(enabled) {
  const ids = [
    'btnUsePatient', 'btnUseInst', 'btnSave', 'btnBatchPatient', 'btnBatchInst',
    'btnSaveInst', 'btnApplyDefault', 'btnSetPatient', 'btnSetInst'
  ];
  ids.forEach((id) => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = !enabled;
  });
}

window.addEventListener('DOMContentLoaded', () => {
  img = document.getElementById('mainImage');
  overlay = document.getElementById('overlay');
  ctx = overlay.getContext('2d');
  previewCanvas = document.getElementById('previewCanvas');
  previewCtx = previewCanvas.getContext('2d');

  function finalizeSelection(displayX, displayY) {
    if (current && current.is_applied) {
      setStatus('This image is already cropped. Selection is disabled.');
      return;
    }
    const sel = normalizeDisplayRectFromPoints(startX, startY, displayX, displayY);
    selectedNat = naturalRectFromDisplay(sel);
    drawOverlay();
    updatePreview();
    setStatus('ROI selected: ' + selectedNat.join(','));
  }

  overlay.addEventListener('mousedown', (e) => {
    if (!current || current.done) return;
    if (current.is_applied) {
      setStatus('This image is already cropped. Drag disabled.');
      return;
    }
    drawing = true;
    const rect = overlay.getBoundingClientRect();
    startX = snapToEdges(clampDisplay(Math.round(e.clientX - rect.left), overlay.width), overlay.width);
    startY = snapToEdges(clampDisplay(Math.round(e.clientY - rect.top), overlay.height), overlay.height);
  });

  overlay.addEventListener('mousemove', (e) => {
    if (!drawing) return;
    const rect = overlay.getBoundingClientRect();
    const x = Math.round(e.clientX - rect.left);
    const y = Math.round(e.clientY - rect.top);
    const sel = normalizeDisplayRectFromPoints(startX, startY, x, y);
    drawOverlay(sel);
  });

  overlay.addEventListener('mouseup', (e) => {
    if (!drawing) return;
    drawing = false;
    const rect = overlay.getBoundingClientRect();
    const x = Math.round(e.clientX - rect.left);
    const y = Math.round(e.clientY - rect.top);
    finalizeSelection(x, y);
  });

  overlay.addEventListener('mouseleave', (e) => {
    if (!drawing) return;
    drawing = false;
    const rect = overlay.getBoundingClientRect();
    const x = Math.round(e.clientX - rect.left);
    const y = Math.round(e.clientY - rect.top);
    finalizeSelection(x, y);
  });

  document.getElementById('btnPrev').onclick = async () => {
    await postAction('/api/prev', {}, null);
  };

  document.getElementById('btnNext').onclick = async () => {
    await postAction('/api/next', {}, null);
  };

  document.getElementById('btnUsePatient').onclick = async () => {
    setSelectedFromRule('patient');
  };

  document.getElementById('btnUseInst').onclick = async () => {
    setSelectedFromRule('inst');
  };

  document.getElementById('btnSave').onclick = async () => {
    if (!requireROI()) return;
    await postAction('/api/save', {roi: selectedNat, set_inst_rule: false}, 'Confirm save crop in-place?');
  };

  document.getElementById('btnBatchPatient').onclick = async () => {
    if (!requireROI()) return;
    await postAction(
      '/api/batch_apply_patient',
      {roi: selectedNat, set_inst_rule: false},
      'Apply this ROI to ALL pending images of the SAME patient?'
    );
  };

  document.getElementById('btnBatchInst').onclick = async () => {
    if (!requireROI()) return;
    await postAction(
      '/api/batch_apply_inst',
      {roi: selectedNat, set_inst_rule: false},
      'Apply this ROI to ALL pending images of the SAME institution+resolution, then jump to nearest unprocessed patient?'
    );
  };

  document.getElementById('btnSaveInst').onclick = async () => {
    if (!requireROI()) return;
    await postAction('/api/save', {roi: selectedNat, set_inst_rule: true}, 'Confirm save crop and set institution+resolution rule?');
  };

  document.getElementById('btnApplyDefault').onclick = async () => {
    if (!current || !current.suggested_roi) {
      setStatus('No suggested rule for current image.');
      return;
    }
    setSelectedFromRule('suggested');
    await postAction('/api/save', {roi: selectedNat, set_inst_rule: false}, 'Confirm save using suggested rule in-place?');
  };

  document.getElementById('btnAutoRules').onclick = async () => {
    await postAction(
      '/api/auto_apply_rules',
      {},
      'Auto-crop ALL unapplied images that already have patient/inst+res rules?'
    );
  };

  document.getElementById('btnSkip').onclick = async () => {
    await postAction('/api/skip', {}, null);
  };

  document.getElementById('btnSetPatient').onclick = async () => {
    if (!requireROI()) return;
    await postAction('/api/set_patient_rule', {roi: selectedNat}, 'Save current ROI as patient rule?');
  };

  document.getElementById('btnSetInst').onclick = async () => {
    if (!requireROI()) return;
    await postAction('/api/set_inst_rule', {roi: selectedNat}, 'Save current ROI as institution+resolution rule?');
  };

  document.getElementById('btnUndo').onclick = async () => {
    await postAction('/api/undo', {}, 'Undo last saved crop?');
  };

  document.getElementById('btnRollback').onclick = async () => {
    await postAction('/api/rollback_all', {}, 'Rollback ALL saved crops in history? This will restore many files. Continue?');
  };

  document.getElementById('btnExit').onclick = async () => {
    await postAction('/api/shutdown', {}, 'Stop crop web server now? Make sure you are done.');
  };

  document.addEventListener('keydown', (e) => {
    // Ignore if currently selecting with mouse drag.
    if (drawing) return;

    const key = (e.key || '').toLowerCase();
    if ((e.ctrlKey || e.metaKey) && key === 's') {
      e.preventDefault();
      clickBtn('btnSave');
      return;
    }

    if (key === 's') { e.preventDefault(); clickBtn('btnSave'); return; }
    if (key === 'b') { e.preventDefault(); clickBtn('btnBatchPatient'); return; }
    if (key === 'g') { e.preventDefault(); clickBtn('btnBatchInst'); return; }
    if (key === 'k') { e.preventDefault(); clickBtn('btnSkip'); return; }
    if (key === 'n' || e.key === 'ArrowRight') { e.preventDefault(); clickBtn('btnNext'); return; }
    if (key === 'p' || e.key === 'ArrowLeft') { e.preventDefault(); clickBtn('btnPrev'); return; }
    if (key === '1') { e.preventDefault(); clickBtn('btnUsePatient'); return; }
    if (key === '2') { e.preventDefault(); clickBtn('btnUseInst'); return; }
    if (key === 'a') { e.preventDefault(); clickBtn('btnAutoRules'); return; }
    if (key === 'd') { e.preventDefault(); clickBtn('btnApplyDefault'); return; }
    if (key === 'u') { e.preventDefault(); clickBtn('btnUndo'); return; }
    if (key === 'r') { e.preventDefault(); clickBtn('btnRollback'); return; }
    if (key === 'x') { e.preventDefault(); clickBtn('btnExit'); return; }
  });

  window.addEventListener('resize', () => {
    if (!img.src) return;
    overlay.width = img.clientWidth;
    overlay.height = img.clientHeight;
    overlay.style.width = img.clientWidth + 'px';
    overlay.style.height = img.clientHeight + 'px';
    drawOverlay();
    updatePreview();
  });

  loadCurrent();
});
</script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    session = None  # type: CropSession

    def _send_json(self, obj: Dict, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json_body(self) -> Dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except Exception:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_image_bytes(self, data: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                self._send_html(HTML_PAGE)
                return

            if path == "/api/current":
                self._send_json(self.session.current_payload())
                return

            if path == "/api/image":
                rel = qs.get("rel", [""])[0]
                rel = unquote(rel)
                data = self.session.read_image_bytes(rel)
                self._send_image_bytes(data)
                return

            self._send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as e:
            self._send_json({"ok": False, "error": repr(e)}, status=400)

    def do_POST(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            body = self._read_json_body()

            if path == "/api/save":
                roi = body.get("roi")
                set_inst_rule = bool(body.get("set_inst_rule", False))
                res = self.session.save_current_crop(roi=roi, set_inst_rule=set_inst_rule)
                self._send_json(res)
                return

            if path == "/api/batch_apply_patient":
                roi = body.get("roi")
                set_inst_rule = bool(body.get("set_inst_rule", False))
                res = self.session.batch_apply_current(
                    roi=roi, scope="patient", set_inst_rule=set_inst_rule
                )
                self._send_json(res)
                return

            if path == "/api/batch_apply_inst":
                roi = body.get("roi")
                set_inst_rule = bool(body.get("set_inst_rule", False))
                res = self.session.batch_apply_current(
                    roi=roi, scope="inst_res", set_inst_rule=set_inst_rule
                )
                self._send_json(res)
                return

            if path == "/api/apply_default":
                set_inst_rule = bool(body.get("set_inst_rule", False))
                res = self.session.apply_default_current(set_inst_rule=set_inst_rule)
                self._send_json(res)
                return

            if path == "/api/auto_apply_rules":
                self._send_json(self.session.auto_apply_existing_rules())
                return

            if path == "/api/skip":
                self._send_json(self.session.skip_current())
                return

            if path == "/api/next":
                self._send_json(self.session.next_image())
                return

            if path == "/api/prev":
                self._send_json(self.session.prev_image())
                return

            if path == "/api/set_patient_rule":
                roi = body.get("roi")
                self._send_json(self.session.set_rule_current(roi=roi, scope="patient"))
                return

            if path == "/api/set_inst_rule":
                roi = body.get("roi")
                self._send_json(self.session.set_rule_current(roi=roi, scope="inst_res"))
                return

            if path == "/api/undo":
                self._send_json(self.session.undo_last())
                return

            if path == "/api/rollback_all":
                self._send_json(self.session.rollback_all())
                return

            if path == "/api/shutdown":
                self._send_json({"ok": True, "message": "Server shutting down..."})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return

            self._send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as e:
            self._send_json({"ok": False, "error": repr(e)}, status=400)


def main() -> int:
    args = parse_args()

    state_json = (
        args.state_json
        if args.state_json is not None
        else (args.public_root / ".crop_session_state.json")
    )
    backup_dir = (
        args.backup_dir
        if args.backup_dir is not None
        else (args.public_root / ".crop_backups")
    )

    if not args.public_root.exists():
        print("[error] public root not found:", args.public_root)
        return 2

    session = CropSession(
        src_root=args.src_root,
        public_root=args.public_root,
        state_json=state_json,
        backup_dir=backup_dir,
        review_all=args.review_all,
        start_index=args.start_index,
    )

    try:
        session.ensure_dependencies()
    except RuntimeError as e:
        print("[error]", e)
        print("Install with: python3 -m pip install --user pydicom pillow")
        return 3

    if args.rollback_all:
        res = session.rollback_all()
        print(res["message"])
        return 0

    AppHandler.session = session
    server = HTTPServer((args.host, int(args.port)), AppHandler)

    print("[info] server started at http://{}:{}".format(args.host, args.port))
    print("[info] src_root:", args.src_root)
    print("[info] public_root:", args.public_root)
    print("[info] state_json:", state_json)
    print("[info] backup_dir:", backup_dir)
    print("[tip] if remote SSH, run local port forward:")
    print("      ssh -L {p}:127.0.0.1:{p} <user>@<server>".format(p=args.port))

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

    print("[info] server stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

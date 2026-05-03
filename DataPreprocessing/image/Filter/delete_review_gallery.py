#!/usr/bin/env python3
"""
Web gallery for keep/delete review on By_Sickid_public images.

- Default modality: pathology
- Supports modality switch: pathology / dicom
- Multi-image page review with pagination
- Click image for zoom preview
- Mark/unmark delete
- Execute deletion safely by moving files into ".delete_trash"
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlparse


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MODALITIES = ("pathology", "dicom")
NAME_FILTERS = ("all", "001002", "not001002", "001", "002")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gallery review tool for delete decisions.")
    parser.add_argument(
        "--public-root",
        type=Path,
        default=Path(
            "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid_public"
        ),
        help="By_Sickid_public root.",
    )
    parser.add_argument(
        "--state-json",
        type=Path,
        default=None,
        help="State file path. Default: <public_root>/.delete_review_state.json",
    )
    parser.add_argument(
        "--trash-dir",
        type=Path,
        default=None,
        help="Trash directory. Default: <public_root>/.delete_trash",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Server host.")
    parser.add_argument("--port", type=int, default=8788, help="Server port.")
    parser.add_argument(
        "--default-page-size",
        type=int,
        default=24,
        help="Default page size (recommended: 24).",
    )
    return parser.parse_args()


def rel_str(path: Path, root: Path) -> str:
    return str(path.relative_to(root).as_posix())


def clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


def iter_files(root: Path) -> Iterable[Path]:
    for dirpath, _, filenames in os.walk(str(root), followlinks=False):
        base = Path(dirpath)
        for name in filenames:
            p = base / name
            if p.is_file():
                yield p


class DeleteReviewSession(object):
    def __init__(self, public_root: Path, state_json: Path, trash_dir: Path):
        self.public_root = public_root
        self.state_json = state_json
        self.trash_dir = trash_dir
        self.state = self._load_state()
        self.files_by_modality = self._scan_files()
        self._cleanup_state_marks()
        self._save_state()

    def _load_state(self) -> Dict:
        if self.state_json.exists():
            with self.state_json.open("r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {}
        data.setdefault("version", 1)
        data.setdefault("delete_marks", {})  # rel_path -> {"time": int}
        data.setdefault("delete_history", [])
        return data

    def _save_state(self) -> None:
        self.state_json.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_json.with_name(self.state_json.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)
        os.replace(str(tmp), str(self.state_json))

    def _detect_modality(self, p: Path) -> str:
        if p.parent.name.lower() == "dicom":
            return "dicom"
        if p.parent.name.lower().startswith("pathology"):
            return "pathology"
        return ""

    def _scan_files(self) -> Dict[str, List[Path]]:
        out = {"pathology": [], "dicom": []}
        for p in iter_files(self.public_root):
            if p.suffix.lower() not in IMAGE_EXTS:
                continue
            rel = rel_str(p, self.public_root)
            parts = rel.split("/")
            if ".crop_backups" in parts:
                continue
            if ".delete_trash" in parts:
                continue
            mod = self._detect_modality(p)
            if mod in out:
                out[mod].append(p)
        out["pathology"].sort(key=lambda x: rel_str(x, self.public_root))
        out["dicom"].sort(key=lambda x: rel_str(x, self.public_root))
        return out

    def _all_rel_set(self) -> set:
        s = set()
        for mod in MODALITIES:
            for p in self.files_by_modality.get(mod, []):
                s.add(rel_str(p, self.public_root))
        return s

    def _cleanup_state_marks(self) -> None:
        keep = self._all_rel_set()
        marks = self.state.get("delete_marks", {})
        stale = [k for k in marks.keys() if k not in keep]
        for k in stale:
            del marks[k]

    def stats(self) -> Dict:
        marks = self.state.get("delete_marks", {})
        by_mod = {}
        for mod in MODALITIES:
            arr = self.files_by_modality.get(mod, [])
            marked = sum(1 for p in arr if rel_str(p, self.public_root) in marks)
            by_mod[mod] = {"total": len(arr), "marked_delete": marked}
        return {"by_modality": by_mod}

    def _match_name_filter(self, rel_path: str, name_filter: str) -> bool:
        if name_filter == "all":
            return True
        stem = Path(rel_path).stem
        if name_filter == "001":
            return bool(re.search(r"_001$", stem))
        if name_filter == "002":
            return bool(re.search(r"_002$", stem))
        if name_filter == "001002":
            return bool(re.search(r"_(001|002)$", stem))
        if name_filter == "not001002":
            return not bool(re.search(r"_(001|002)$", stem))
        return True

    def list_page(
        self,
        modality: str,
        page: int,
        page_size: int,
        name_filter: str = "all",
        anchor_global_index: int = 0,
    ) -> Dict:
        if modality not in MODALITIES:
            raise RuntimeError("Unknown modality: {}".format(modality))
        if name_filter not in NAME_FILTERS:
            raise RuntimeError("Unknown name_filter: {}".format(name_filter))
        page_size = clamp(int(page_size), 6, 120)

        arr_all = self.files_by_modality.get(modality, [])
        filtered: List[Tuple[int, Path]] = []
        for i, p in enumerate(arr_all, 1):
            rel = rel_str(p, self.public_root)
            if self._match_name_filter(rel, name_filter):
                filtered.append((i, p))

        total = len(filtered)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = clamp(int(page), 1, total_pages)

        anchor_global_index = int(anchor_global_index or 0)
        nearest_filtered_index = 0  # 1-based position in filtered list
        if anchor_global_index > 0 and total > 0:
            nearest_pos = min(
                range(len(filtered)),
                key=lambda pos: abs(filtered[pos][0] - anchor_global_index),
            )
            nearest_filtered_index = nearest_pos + 1
            page = (nearest_pos // page_size) + 1

        start = (page - 1) * page_size
        end = min(total, start + page_size)
        marks = self.state.get("delete_marks", {})

        items = []
        for local_i in range(start, end):
            global_i, p = filtered[local_i]
            rel = rel_str(p, self.public_root)
            items.append(
                {
                    "index": local_i + 1,
                    "global_index": global_i,
                    "rel_path": rel,
                    "image_url": "/api/image?rel=" + quote(rel),
                    "marked_delete": rel in marks,
                }
            )

        st_mod = self.stats()["by_modality"][modality]
        marked_filtered = sum(1 for _, p in filtered if rel_str(p, self.public_root) in marks)
        return {
            "ok": True,
            "modality": modality,
            "name_filter": name_filter,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "marked_delete_total": marked_filtered,
            "marked_delete_total_modality": st_mod["marked_delete"],
            "nearest_filtered_index": nearest_filtered_index,
            "anchor_global_index_used": anchor_global_index,
            "items": items,
        }

    def toggle_delete(self, rel_path: str) -> Dict:
        rel_path = rel_path.lstrip("/")
        target = (self.public_root / rel_path).resolve()
        root = self.public_root.resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise RuntimeError("Invalid path")
        if not target.exists() or not target.is_file():
            raise RuntimeError("File not found")
        if target.suffix.lower() not in IMAGE_EXTS:
            raise RuntimeError("Not an image file")

        marks = self.state.get("delete_marks", {})
        if rel_path in marks:
            del marks[rel_path]
            marked = False
        else:
            marks[rel_path] = {"time": int(time.time())}
            marked = True
        self._save_state()
        return {"ok": True, "marked_delete": marked, "rel_path": rel_path}

    def save_marks(self) -> Dict:
        self._cleanup_state_marks()
        self._save_state()
        return {
            "ok": True,
            "message": "Saved marks (count={})".format(len(self.state.get("delete_marks", {}))),
        }

    def apply_delete(
        self,
        modality: str,
        progress_cb: Optional[Callable[[int, int, str, int, int], None]] = None,
    ) -> Dict:
        if modality not in MODALITIES:
            raise RuntimeError("Unknown modality: {}".format(modality))

        marks = self.state.get("delete_marks", {})
        candidates = []
        for p in self.files_by_modality.get(modality, []):
            rel = rel_str(p, self.public_root)
            if rel in marks:
                candidates.append((p, rel))

        moved = 0
        failed = []
        stamp = str(int(time.time()))
        total = len(candidates)
        for i, (p, rel) in enumerate(candidates, 1):
            try:
                dst = self.trash_dir / stamp / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(p), str(dst))
                moved += 1
                if rel in marks:
                    del marks[rel]
                self.state["delete_history"].append(
                    {
                        "time": int(time.time()),
                        "rel_path": rel,
                        "moved_to": str(dst),
                    }
                )
            except Exception as e:
                failed.append("{} => {}".format(rel, repr(e)))
            if progress_cb is not None:
                try:
                    progress_cb(i, total, rel, moved, len(failed))
                except Exception:
                    pass

        self.files_by_modality = self._scan_files()
        self._cleanup_state_marks()
        self._save_state()

        msg = "Moved {} file(s) to trash under .delete_trash".format(moved)
        if failed:
            msg += " | failed={}".format(len(failed))
            msg += " | sample_fail: " + "; ".join(failed[:3])
        return {"ok": True, "message": msg, "moved": moved, "failed": len(failed)}

    def read_image_bytes(self, rel_path: str) -> bytes:
        rel_path = rel_path.lstrip("/")
        target = (self.public_root / rel_path).resolve()
        root = self.public_root.resolve()
        if not str(target).startswith(str(root) + os.sep):
            raise RuntimeError("Invalid path")
        if not target.exists() or not target.is_file():
            raise RuntimeError("Image not found")
        if target.suffix.lower() not in IMAGE_EXTS:
            raise RuntimeError("Only images can be served")
        return target.read_bytes()


HTML_PAGE = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Delete Review Gallery</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: #f3f4f6; color: #111; }
    .top { background: #0f172a; color: #fff; padding: 10px 14px; }
    .toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 10px 14px; background: #fff; border-bottom: 1px solid #ddd; }
    select, input, button { padding: 8px 10px; border: 1px solid #bbb; border-radius: 6px; background: #fff; }
    button.primary { background: #2563eb; color: #fff; border-color: #2563eb; }
    button.warn { background: #b91c1c; color: #fff; border-color: #b91c1c; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    .stats { padding: 8px 14px; font-size: 13px; color: #333; }
    .grid { display: grid; gap: 10px; padding: 10px 14px 16px; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); }
    .card { background: #fff; border: 1px solid #d1d5db; border-radius: 8px; overflow: hidden; }
    .card.marked { border: 2px solid #dc2626; box-shadow: 0 0 0 2px rgba(220,38,38,0.15); }
    .thumb { width: 100%; height: 170px; object-fit: contain; background: #000; display: block; cursor: zoom-in; }
    .meta { padding: 8px; font-size: 12px; line-height: 1.4; color: #222; word-break: break-all; min-height: 52px; }
    .row { display: flex; gap: 8px; padding: 0 8px 8px; }
    .row button { flex: 1; }
    .pager { margin-left: auto; display: flex; align-items: center; gap: 8px; }
    .status { padding: 8px 14px; font-size: 13px; color: #111; background: #fffbe6; border-top: 1px solid #eee; border-bottom: 1px solid #eee; white-space: pre-wrap; }
    .empty { padding: 20px; text-align: center; color: #666; }

    .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.82); display: none; align-items: center; justify-content: center; z-index: 99; }
    .modal.show { display: flex; }
    .modal img { max-width: 92vw; max-height: 92vh; object-fit: contain; background: #000; border: 1px solid #222; }
    .hint { font-size: 12px; color: #444; }
  </style>
</head>
<body>
  <div class="top"><b>Delete Review Gallery</b> - quick keep/delete scan (default pathology)</div>

  <div class="toolbar">
    <label>Modality:
      <select id="modality">
        <option value="pathology" selected>pathology</option>
        <option value="dicom">dicom</option>
      </select>
    </label>
    <label>Per page:
      <select id="pageSize">
        <option value="12">12</option>
        <option value="24" selected>24</option>
        <option value="36">36</option>
      </select>
    </label>

    <button id="btnPrev">Prev</button>
    <button id="btnNext">Next</button>
    <label>Go page:
      <input id="pageInput" type="number" min="1" value="1" style="width:90px;" />
    </label>
    <button id="btnGo">Go</button>

    <button class="warn" id="btnApplyDelete">Move Marked (Current Modality) to Trash</button>

    <div class="pager">
      <span id="pagerText">page 1/1</span>
    </div>
  </div>

  <div class="stats" id="stats">Loading...</div>
  <div class="status" id="status">Ready.</div>
  <div id="grid" class="grid"></div>
  <div class="hint" style="padding:0 14px 14px;">
    Tip: Click image to zoom. Mark only a few for delete. Left/Right arrow switches page.
  </div>

  <div id="modal" class="modal" onclick="closeModal()">
    <img id="modalImg" alt="zoom" />
  </div>

<script>
let current = { modality: 'pathology', page: 1, page_size: 24, total_pages: 1 };

function setStatus(msg) {
  document.getElementById('status').textContent = msg;
}

async function fetchJSON(url, options={}) {
  const res = await fetch(url, options);
  const data = await res.json();
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || data.message || 'Request failed');
  }
  return data;
}

function openModal(url) {
  const modal = document.getElementById('modal');
  const img = document.getElementById('modalImg');
  img.src = url + '&zoom=' + Date.now();
  modal.classList.add('show');
}

function closeModal() {
  document.getElementById('modal').classList.remove('show');
}

async function loadPage() {
  const mod = document.getElementById('modality').value;
  const size = parseInt(document.getElementById('pageSize').value, 10) || 24;
  const page = Math.max(1, parseInt(document.getElementById('pageInput').value, 10) || 1);

  try {
    const data = await fetchJSON(`/api/list?modality=${encodeURIComponent(mod)}&page=${page}&page_size=${size}`);
    current = data;
    document.getElementById('pageInput').value = current.page;
    document.getElementById('pagerText').textContent = `page ${current.page}/${current.total_pages}`;

    const stats = `modality=${current.modality} | total=${current.total} | marked_delete=${current.marked_delete_total} | page_size=${current.page_size}`;
    document.getElementById('stats').textContent = stats;

    const grid = document.getElementById('grid');
    grid.innerHTML = '';
    if (!current.items || current.items.length === 0) {
      grid.innerHTML = '<div class="empty">No images in this modality.</div>';
      return;
    }

    current.items.forEach((it) => {
      const card = document.createElement('div');
      card.className = 'card' + (it.marked_delete ? ' marked' : '');
      const btnText = it.marked_delete ? 'Unmark Delete' : 'Mark Delete';

      card.innerHTML = `
        <img loading="lazy" class="thumb" src="${it.image_url}" alt="${it.rel_path}" />
        <div class="meta">#${it.index}<br/>${it.rel_path}</div>
        <div class="row">
          <button data-rel="${it.rel_path}" class="btnToggle">${btnText}</button>
        </div>
      `;
      const img = card.querySelector('.thumb');
      img.onclick = () => openModal(it.image_url);
      const btn = card.querySelector('.btnToggle');
      btn.onclick = () => toggleDelete(it.rel_path);
      grid.appendChild(card);
    });
  } catch (e) {
    setStatus('loadPage error: ' + e.message);
  }
}

async function toggleDelete(relPath) {
  try {
    const data = await fetchJSON('/api/toggle_delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ rel_path: relPath })
    });
    setStatus(`${data.marked_delete ? 'Marked' : 'Unmarked'} delete: ${relPath}`);
    await loadPage();
  } catch (e) {
    setStatus('toggle error: ' + e.message);
  }
}

async function applyDelete() {
  const mod = document.getElementById('modality').value;
  if (!window.confirm(`Move all MARKED images in ${mod} to .delete_trash ?`)) return;
  try {
    const data = await fetchJSON('/api/apply_delete', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ modality: mod })
    });
    setStatus(data.message || 'Done.');
    await loadPage();
  } catch (e) {
    setStatus('apply_delete error: ' + e.message);
  }
}

window.addEventListener('DOMContentLoaded', () => {
  document.getElementById('modality').onchange = () => {
    document.getElementById('pageInput').value = '1';
    loadPage();
  };
  document.getElementById('pageSize').onchange = () => {
    document.getElementById('pageInput').value = '1';
    loadPage();
  };
  document.getElementById('btnPrev').onclick = () => {
    const p = Math.max(1, (current.page || 1) - 1);
    document.getElementById('pageInput').value = String(p);
    loadPage();
  };
  document.getElementById('btnNext').onclick = () => {
    const p = Math.min(current.total_pages || 1, (current.page || 1) + 1);
    document.getElementById('pageInput').value = String(p);
    loadPage();
  };
  document.getElementById('btnGo').onclick = () => loadPage();
  document.getElementById('btnApplyDelete').onclick = () => applyDelete();

  document.addEventListener('keydown', (e) => {
    if (document.getElementById('modal').classList.contains('show')) {
      if (e.key === 'Escape') {
        closeModal();
      }
      return;
    }
    if (e.key === 'ArrowLeft') {
      document.getElementById('btnPrev').click();
    } else if (e.key === 'ArrowRight') {
      document.getElementById('btnNext').click();
    }
  });

  loadPage();
});
</script>
</body>
</html>
"""


class AppHandler(BaseHTTPRequestHandler):
    session = None  # type: DeleteReviewSession

    def _send_json(self, obj: Dict, status: int = 200) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_image_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

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

    def do_GET(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path in ("/", "/index.html"):
                self._send_html(HTML_PAGE)
                return

            if path == "/api/list":
                mod = qs.get("modality", ["pathology"])[0]
                page = int(qs.get("page", ["1"])[0])
                page_size = int(qs.get("page_size", ["24"])[0])
                name_filter = qs.get("name_filter", ["all"])[0]
                anchor_global_index = int(qs.get("anchor_global_index", ["0"])[0])
                self._send_json(
                    self.session.list_page(
                        modality=mod,
                        page=page,
                        page_size=page_size,
                        name_filter=name_filter,
                        anchor_global_index=anchor_global_index,
                    )
                )
                return

            if path == "/api/image":
                rel = unquote(qs.get("rel", [""])[0])
                data = self.session.read_image_bytes(rel)
                suffix = Path(rel).suffix.lower()
                mime = {
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".png": "image/png",
                    ".bmp": "image/bmp",
                    ".webp": "image/webp",
                }.get(suffix, "application/octet-stream")
                self._send_image_bytes(data, mime)
                return

            self._send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as e:
            self._send_json({"ok": False, "error": repr(e)}, status=400)

    def do_POST(self):  # noqa: N802
        try:
            path = urlparse(self.path).path
            body = self._read_json_body()

            if path == "/api/toggle_delete":
                rel_path = str(body.get("rel_path", ""))
                self._send_json(self.session.toggle_delete(rel_path))
                return

            if path == "/api/apply_delete":
                mod = str(body.get("modality", "pathology"))
                self._send_json(self.session.apply_delete(modality=mod))
                return

            self._send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as e:
            self._send_json({"ok": False, "error": repr(e)}, status=400)


def main() -> int:
    args = parse_args()
    public_root = args.public_root
    if not public_root.exists():
        print("[error] public root not found:", public_root)
        return 2

    state_json = (
        args.state_json
        if args.state_json is not None
        else (public_root / ".delete_review_state.json")
    )
    trash_dir = (
        args.trash_dir
        if args.trash_dir is not None
        else (public_root / ".delete_trash")
    )

    session = DeleteReviewSession(
        public_root=public_root,
        state_json=state_json,
        trash_dir=trash_dir,
    )
    AppHandler.session = session

    server = HTTPServer((args.host, int(args.port)), AppHandler)
    print("[info] delete review server started at http://{}:{}".format(args.host, args.port))
    print("[info] public_root:", public_root)
    print("[info] state_json:", state_json)
    print("[info] trash_dir:", trash_dir)
    st = session.stats()["by_modality"]
    print("[info] pathology total={}, marked_delete={}".format(st["pathology"]["total"], st["pathology"]["marked_delete"]))
    print("[info] dicom total={}, marked_delete={}".format(st["dicom"]["total"], st["dicom"]["marked_delete"]))
    print("[tip] default modality in UI: pathology | recommended per-page: 24")
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

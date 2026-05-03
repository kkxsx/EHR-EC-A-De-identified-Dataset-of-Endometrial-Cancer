#!/usr/bin/env python3
"""
Streamlit gallery for keep/delete review on By_Sickid_public images.

Features:
- Default modality: pathology (switchable to dicom)
- Per-page review (12 / 24 / 36)
- Quick zoom preview
- Mark/unmark delete
- Apply delete by moving marked files into .delete_trash
"""

import io
import hashlib
import time
from pathlib import Path
from typing import Tuple

import streamlit as st
from PIL import Image

from delete_review_gallery import DeleteReviewSession


DEFAULT_PUBLIC_ROOT = Path(
    "/media/dell/426e651e-217f-47f6-97e1-d88a74140af1/data_med/By_Sickid_public"
)


@st.cache_resource(show_spinner=False)
def get_session(public_root: str, state_json: str, trash_dir: str) -> DeleteReviewSession:
    return DeleteReviewSession(
        public_root=Path(public_root),
        state_json=Path(state_json),
        trash_dir=Path(trash_dir),
    )


def init_state() -> None:
    st.session_state.setdefault("modality", "pathology")
    st.session_state.setdefault("page_size", 24)
    st.session_state.setdefault("page", 1)
    st.session_state.setdefault("name_filter", "all")
    st.session_state.setdefault("zoom_rel", "")
    st.session_state.setdefault("confirm_delete", False)
    st.session_state.setdefault("compact_mode", True)
    st.session_state.setdefault("show_full_path", False)
    st.session_state.setdefault("mark_dirty", False)
    st.session_state.setdefault("last_view_key", ("pathology", "all"))
    st.session_state.setdefault("anchor_idx_pathology", 1)
    st.session_state.setdefault("anchor_idx_dicom", 1)


def apply_compact_css() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 0.5rem; padding-bottom: 0.4rem; }
        div[data-testid="stVerticalBlock"] { gap: 0.35rem; }
        .stCaption { margin-top: 0.1rem; margin-bottom: 0.1rem; }
        button[kind] { min-height: 1.9rem; padding-top: 0.1rem; padding-bottom: 0.1rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )


@st.cache_data(show_spinner=False, max_entries=6000)
def load_thumb_bytes(public_root: str, rel_path: str, max_side: int, mtime_ns: int) -> bytes:
    # mtime_ns is included only for cache invalidation.
    _ = mtime_ns
    p = Path(public_root) / rel_path
    with Image.open(str(p)) as im:
        if im.mode != "RGB":
            im = im.convert("RGB")
        resampling = getattr(Image, "Resampling", Image)
        im.thumbnail((max_side, max_side), resampling.LANCZOS)
        bio = io.BytesIO()
        # Favor generation speed over perfect quality for gallery thumbnails.
        im.save(bio, format="JPEG", quality=68, optimize=False)
        return bio.getvalue()


@st.cache_data(show_spinner=False, max_entries=6000)
def load_full_bytes(public_root: str, rel_path: str, mtime_ns: int) -> bytes:
    _ = mtime_ns
    return (Path(public_root) / rel_path).read_bytes()


def top_controls(total_pages: int) -> Tuple[str, str, int, int]:
    c1, c2, c3, c4, c5, c6, c7 = st.columns([1.1, 1.1, 1, 1, 1, 1, 1.3])
    with c1:
        st.session_state.modality = st.selectbox(
            "Modality",
            options=["pathology", "dicom"],
            index=0 if st.session_state.modality == "pathology" else 1,
        )
    with c2:
        filter_options = {
            "all": "All",
            "001002": "001+002",
            "not001002": "Exclude 001+002",
            "001": "Only 001",
            "002": "Only 002",
        }
        inv_filter_options = {v: k for k, v in filter_options.items()}
        selected_label = st.selectbox(
            "Name Filter",
            options=list(filter_options.values()),
            index=list(filter_options.keys()).index(st.session_state.name_filter)
            if st.session_state.name_filter in filter_options
            else 0,
        )
        st.session_state.name_filter = inv_filter_options[selected_label]
    with c3:
        st.session_state.page_size = st.selectbox(
            "Per page",
            options=[12, 24, 36],
            index=[12, 24, 36].index(st.session_state.page_size)
            if st.session_state.page_size in (12, 24, 36)
            else 1,
        )
    with c4:
        if st.button("Prev"):
            st.session_state.page = max(1, int(st.session_state.page) - 1)
    with c5:
        if st.button("Next"):
            st.session_state.page = min(max(1, int(total_pages)), int(st.session_state.page) + 1)
    with c6:
        st.session_state.page = st.number_input(
            "Page",
            min_value=1,
            max_value=max(1, int(total_pages)),
            value=min(max(1, int(st.session_state.page)), max(1, int(total_pages))),
            step=1,
        )
    with c7:
        st.caption("Tip: 24/page is a good default")
    return (
        st.session_state.modality,
        st.session_state.name_filter,
        int(st.session_state.page),
        int(st.session_state.page_size),
    )


def render_zoom(session: DeleteReviewSession) -> None:
    rel = st.session_state.get("zoom_rel", "")
    if not rel:
        return
    st.markdown("### Zoom")
    st.write(rel)
    try:
        p = session.public_root / rel
        mtime_ns = int(p.stat().st_mtime_ns) if p.exists() else 0
        st.image(load_full_bytes(str(session.public_root), rel, mtime_ns), width="stretch")
    except Exception as e:
        st.error("Failed to load zoom image: {}".format(repr(e)))
    if st.button("Close Zoom"):
        st.session_state.zoom_rel = ""
        st.rerun()


def _mark_widget_key(rel_path: str) -> str:
    return "del_" + hashlib.md5(rel_path.encode("utf-8")).hexdigest()


def _toggle_mark_in_memory(session: DeleteReviewSession, rel_path: str, mark: bool) -> None:
    marks = session.state.setdefault("delete_marks", {})
    if mark:
        marks[rel_path] = {"time": int(time.time())}
    else:
        marks.pop(rel_path, None)
    st.session_state.mark_dirty = True


def _flush_marks(session: DeleteReviewSession) -> None:
    # Backward compatibility:
    # if an older cached session object is still alive (without save_marks),
    # fallback to internal save behavior.
    if hasattr(session, "save_marks"):
        session.save_marks()
    elif hasattr(session, "_save_state"):
        session._save_state()
    else:
        raise RuntimeError("Session object cannot persist marks.")
    st.session_state.mark_dirty = False


def _reset_mark_widget_cache() -> None:
    for k in list(st.session_state.keys()):
        if k.startswith("del_"):
            del st.session_state[k]


def main() -> None:
    st.set_page_config(page_title="Delete Review Gallery", layout="wide")
    apply_compact_css()
    st.title("Delete Review Gallery")
    st.caption("Fast keep/delete scan. Default modality is pathology.")

    init_state()

    with st.sidebar:
        st.subheader("Data Paths")
        public_root = st.text_input("public_root", str(DEFAULT_PUBLIC_ROOT))
        state_json = st.text_input(
            "state_json",
            str(Path(public_root) / ".delete_review_state.json"),
        )
        trash_dir = st.text_input(
            "trash_dir",
            str(Path(public_root) / ".delete_trash"),
        )
        if st.button("Reload Session"):
            if st.session_state.mark_dirty:
                st.warning("Unsaved marks were discarded on reload.")
            get_session.clear()
            load_thumb_bytes.clear()
            load_full_bytes.clear()
            st.session_state.mark_dirty = False
            _reset_mark_widget_cache()
            st.rerun()
        st.session_state.compact_mode = st.checkbox(
            "Compact one-screen mode",
            value=st.session_state.compact_mode,
        )
        st.session_state.show_full_path = st.checkbox(
            "Show full path on cards",
            value=st.session_state.show_full_path,
        )

    try:
        session = get_session(public_root, state_json, trash_dir)
    except Exception as e:
        st.error("Failed to init session: {}".format(repr(e)))
        return

    stats = session.stats()["by_modality"]
    st.info(
        "pathology total={} (marked={}) | dicom total={} (marked={})".format(
            stats["pathology"]["total"],
            stats["pathology"]["marked_delete"],
            stats["dicom"]["total"],
            stats["dicom"]["marked_delete"],
        )
    )

    modality, name_filter, page, page_size = top_controls(total_pages=999999)
    current_view_key = (modality, name_filter)
    view_changed = current_view_key != tuple(st.session_state.get("last_view_key", ("pathology", "all")))
    anchor_key = "anchor_idx_{}".format(modality)
    anchor_idx = int(st.session_state.get(anchor_key, 1))
    try:
        payload = session.list_page(
            modality=modality,
            page=page,
            page_size=page_size,
            name_filter=name_filter,
            anchor_global_index=(anchor_idx if view_changed else 0),
        )
    except Exception as e:
        st.error("Failed to load page: {}".format(repr(e)))
        return

    # Normalize page after backend clamp.
    st.session_state.page = int(payload["page"])
    st.session_state.last_view_key = current_view_key
    if payload.get("items"):
        st.session_state[anchor_key] = int(payload["items"][0].get("global_index", 1))

    st.write(
        "Filter={} | Page {}/{} | total={} | marked_delete(filtered)={} | marked_delete(modality)={}".format(
            payload.get("name_filter", name_filter),
            payload["page"],
            payload["total_pages"],
            payload["total"],
            payload["marked_delete_total"],
            payload.get("marked_delete_total_modality", payload["marked_delete_total"]),
        )
    )
    if st.session_state.mark_dirty:
        st.warning("You have unsaved mark changes. Click 'Save Marks' to persist to disk.")

    st.divider()
    render_zoom(session)

    cdel1, cdel2, cdel3 = st.columns([2, 1, 1.3])
    with cdel1:
        st.session_state.confirm_delete = st.checkbox(
            "Confirm moving MARKED files of current modality into .delete_trash",
            value=st.session_state.confirm_delete,
        )
    with cdel2:
        if st.button("Save Marks"):
            try:
                _flush_marks(session)
                st.success("Marks saved.")
            except Exception as e:
                st.error("save marks failed: {}".format(repr(e)))
    with cdel3:
        if st.button(
            "Apply Delete (Current Modality)",
            disabled=not st.session_state.confirm_delete,
        ):
            try:
                if st.session_state.mark_dirty:
                    _flush_marks(session)
                total_marked = int(payload.get("marked_delete_total", 0))
                if total_marked <= 0:
                    st.info("No marked images to delete in current modality.")
                progress_bar = st.progress(0)
                progress_text = st.empty()
                throttle = {"last_shown": 0}

                def on_progress(done: int, total: int, _rel: str, moved: int, failed: int) -> None:
                    if total <= 0:
                        progress_bar.progress(100)
                        progress_text.write("No marked files.")
                        return
                    # Throttle UI updates to reduce overhead.
                    if done < total and (done - throttle["last_shown"]) < 12:
                        return
                    throttle["last_shown"] = done
                    pct = int((done * 100) / total)
                    progress_bar.progress(pct)
                    progress_text.write(
                        "Deleting {} / {} | moved={} | failed={}".format(done, total, moved, failed)
                    )

                res = session.apply_delete(modality=modality, progress_cb=on_progress)
                progress_bar.progress(100)
                progress_text.write("Delete finished.")
                st.success(res.get("message", "Done"))
                st.session_state.confirm_delete = False
                _reset_mark_widget_cache()
                st.rerun()
            except Exception as e:
                st.error("apply_delete failed: {}".format(repr(e)))

    items = payload.get("items", [])
    if not items:
        st.warning("No images in this modality.")
        return

    if st.session_state.compact_mode:
        if page_size >= 36:
            cols_n = 9
            thumb_side = 145
        elif page_size >= 24:
            cols_n = 8
            thumb_side = 155
        else:
            cols_n = 6
            thumb_side = 185
    else:
        if page_size >= 36:
            cols_n = 7
            thumb_side = 195
        elif page_size >= 24:
            cols_n = 6
            thumb_side = 210
        else:
            cols_n = 4
            thumb_side = 260

    cols = st.columns(cols_n)
    for i, it in enumerate(items):
        rel = it["rel_path"]
        marked = bool(it.get("marked_delete", False))
        with cols[i % cols_n]:
            try:
                p = session.public_root / rel
                mtime_ns = int(p.stat().st_mtime_ns) if p.exists() else 0
                thumb = load_thumb_bytes(str(session.public_root), rel, thumb_side, mtime_ns)
                st.image(thumb, width="stretch")
            except Exception as e:
                st.error("Image load fail: {}".format(repr(e)))
            if st.session_state.show_full_path:
                cap = "#{} {}".format(it.get("global_index", it["index"]), rel)
            else:
                cap = "#{} {}".format(it.get("global_index", it["index"]), Path(rel).name)
            st.caption(cap)
            b1, b2 = st.columns(2)
            with b1:
                if st.button("Zoom", key="zoom_{}".format(rel)):
                    st.session_state.zoom_rel = rel
                    st.rerun()
            with b2:
                key = _mark_widget_key(rel)
                if key not in st.session_state:
                    st.session_state[key] = marked
                ui_mark = st.checkbox(
                    "Delete",
                    key=key,
                    label_visibility="collapsed",
                    help="Check to mark this image for deletion",
                )
                if ui_mark != marked:
                    _toggle_mark_in_memory(session, rel, ui_mark)


if __name__ == "__main__":
    main()

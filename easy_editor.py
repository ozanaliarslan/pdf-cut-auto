from __future__ import annotations

import json
import time
import hashlib
import shutil
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pypdf import PdfReader
from binary_resolver import get_binary_path

import sys
import threading

class WebAppProxy:
    def __getattr__(self, name: str) -> Any:
        import web_app
        return getattr(web_app, name)

web_app = WebAppProxy()


def _cached_page_data(xml_path: Path, page_idx: int, pdf_page: Any | None = None) -> Any:
    from agents.base_agent import Item, PageData, normalize_space

    root = ET.parse(xml_path).getroot()
    page_el = root.find("page")
    if page_el is None:
        raise ValueError(f"Sayfa XML verisi bulunamadı: {xml_path}")

    font_sizes = {
        font.attrib.get("id", ""): float(font.attrib.get("size", "0") or 0)
        for font in page_el.findall("fontspec")
    }
    items = []
    for child in page_el:
        if child.tag not in {"text", "image"}:
            continue
        items.append(
            Item(
                kind=child.tag,
                top=float(child.attrib.get("top", "0")),
                left=float(child.attrib.get("left", "0")),
                width=float(child.attrib.get("width", "0")),
                height=float(child.attrib.get("height", "0")),
                text=normalize_space("".join(child.itertext())) if child.tag == "text" else "",
                font_size=font_sizes.get(child.attrib.get("font", ""), 0.0),
            )
        )

    width_px = float(page_el.attrib.get("width", "0"))
    height_px = float(page_el.attrib.get("height", "0"))
    return PageData(
        number=page_idx + 1,
        width_px=width_px,
        height_px=height_px,
        width_pt=float(pdf_page.mediabox.width) if pdf_page is not None else width_px,
        height_pt=float(pdf_page.mediabox.height) if pdf_page is not None else height_px,
        items=items,
    )


def _deneme_number_from_page(page: Any) -> int | None:
    from agents.base_agent import normalize_space

    header_items = [
        item
        for item in sorted(page.texts, key=lambda value: (value.top, value.left))
        if item.top <= min(360.0, page.height_px * 0.42)
    ]
    for item in header_items:
        direct_text = normalize_space(item.text).upper().replace("İ", "I")
        direct_match = re.search(r"\bDENEME\s*[-:]?\s*(\d{1,3})\b", direct_text)
        if direct_match:
            return int(direct_match.group(1))

    marker_items = [
        item for item in header_items
        if "DENEME" in normalize_space(item.text).upper().replace("İ", "I")
    ]
    number_items = [
        item for item in header_items
        if re.fullmatch(r"\d{1,3}", normalize_space(item.text))
    ]
    nearby_numbers = []
    for marker in marker_items:
        for number in number_items:
            vertical_gap = abs(number.top - marker.top)
            horizontal_gap = abs(number.center_x - marker.center_x)
            if vertical_gap > 55.0 or horizontal_gap > 130.0:
                continue
            nearby_numbers.append((vertical_gap * 2 + horizontal_gap, int(normalize_space(number.text))))
    if nearby_numbers:
        return min(nearby_numbers)[1]

    header_text = " ".join(normalize_space(item.text) for item in header_items if item.text)
    normalized = header_text.upper().replace("İ", "I")
    match = re.search(r"\bDENEME\s*[-:]?\s*(\d{1,3})\b", normalized)
    if not match:
        match = re.search(r"\b(\d{1,3})\s*\.?\s*DENEME\b", normalized)
    return int(match.group(1)) if match else None


def build_test_navigation_from_pages(pages: list[Any]) -> list[dict[str, int | str]]:
    """Detect test boundaries without creating any question crops."""
    from soru_kesim_pdf_only import detect_subject, select_test_structure

    if not pages:
        return []

    explicit_starts: list[tuple[int, int]] = []
    for page in pages:
        deneme_no = _deneme_number_from_page(page)
        if deneme_no is None:
            continue
        if explicit_starts and explicit_starts[-1][1] == deneme_no:
            continue
        explicit_starts.append((page.number, deneme_no))

    if explicit_starts:
        return [
            {
                "test_no": index,
                "label": f"Deneme {deneme_no}",
                "start_page_idx": start_page - 1,
                "end_page_idx": (explicit_starts[index][0] - 2) if index < len(explicit_starts) else pages[-1].number - 1,
            }
            for index, (start_page, deneme_no) in enumerate(explicit_starts, start=1)
        ]

    subject = detect_subject(pages[0])
    tests, _source = select_test_structure(
        pages,
        [],
        pages[-1].number,
        subject,
        module_activity_mode=True,
    )
    navigation = []
    for test in tests:
        start_page = next((page for page in pages if page.number == test.start_page), None)
        deneme_no = _deneme_number_from_page(start_page) if start_page is not None else None
        navigation.append(
            {
                "test_no": int(test.test_no),
                "label": f"Deneme {deneme_no if deneme_no is not None else test.test_no}",
                "start_page_idx": int(test.start_page) - 1,
                "end_page_idx": int(test.end_page) - 1,
            }
        )
    return navigation


def detect_editor_test_navigation(
    job_dir: Path,
    pdf_name: str,
    pdf_path: Path,
    page_count: int | None = None,
) -> list[dict[str, int | str]]:
    cache_dir = job_dir / "preview_cache"
    web_app.ensure_dir(cache_dir)
    cache_path = cache_dir / "test_navigation.json"
    pdf_stat = pdf_path.stat()
    fingerprint = f"{pdf_stat.st_size}:{pdf_stat.st_mtime_ns}"

    if cache_path.exists():
        cached = web_app.read_json_file(cache_path)
        if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
            tests = cached.get("tests")
            if isinstance(tests, list):
                return tests

    if page_count is None:
        page_count = len(PdfReader(pdf_path).pages)
    pages = []
    row = {"source_pdf": pdf_name}
    for page_idx in range(page_count):
        web_app.source_page_layout_for_row(job_dir, row, page_idx)
        xml_path = cache_dir / f"source_page_{page_idx + 1}.xml"
        pages.append(_cached_page_data(xml_path, page_idx))

    tests = build_test_navigation_from_pages(pages)
    cache_path.write_text(
        json.dumps({"fingerprint": fingerprint, "tests": tests}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return tests


def parse_selected_page_indices(pages_str: str | None, total_pages: int) -> set[int] | None:
    if not pages_str:
        return None
    pages_str = str(pages_str).strip()
    if not pages_str:
        return None
        
    indices = set()
    parts = pages_str.split(",")
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start_str, end_str = part.split("-", 1)
                start = int(start_str.strip())
                end = int(end_str.strip())
                for p in range(start, end + 1):
                    if 1 <= p <= total_pages:
                        indices.add(p - 1)
            except ValueError:
                continue
        else:
            try:
                p = int(part)
                if 1 <= p <= total_pages:
                    indices.add(p - 1)
            except ValueError:
                continue
    return indices


def filter_non_body_items(text_items: list[dict], page_width: float, page_height: float, page_idx: int) -> list[dict]:
    from agents.base_agent import Item, PageData
    from agents.layout_agent import LayoutAgent

    items = []
    for it in text_items:
        try:
            items.append(
                Item(
                    kind="text",
                    top=float(it.get("top", 0)),
                    left=float(it.get("left", 0)),
                    width=float(it.get("right", 0)) - float(it.get("left", 0)),
                    height=float(it.get("bottom", 0)) - float(it.get("top", 0)),
                    text=it.get("text", ""),
                    font_size=float(it.get("font_size", 0.0) or it.get("size", 0.0) or 0.0),
                )
            )
        except Exception:
            continue

    page = PageData(
        number=page_idx + 1,
        width_px=page_width,
        height_px=page_height,
        width_pt=page_width,
        height_pt=page_height,
        items=items,
    )

    layout_agent = LayoutAgent()
    filtered_items = []
    for it in text_items:
        try:
            left = float(it["left"])
            top = float(it["top"])
            right = float(it["right"])
            bottom = float(it["bottom"])
            current_item = Item(
                kind="text",
                top=top,
                left=left,
                width=right - left,
                height=bottom - top,
                text=it.get("text", ""),
                font_size=float(it.get("font_size", 0.0) or it.get("size", 0.0) or 0.0),
            )
            # Exclude header/footer items
            if layout_agent.is_footer_item(page, current_item) or layout_agent.is_header_item(page, current_item):
                continue
            filtered_items.append(it)
        except Exception:
            filtered_items.append(it)
            
    return filtered_items


def detect_questions_from_markers(
    job_dir: Path,
    pdf_name: str,
    page_idx: int,
    markers: list[dict],
    test_no: int = 1,
    test_name: str = "Test-1"
) -> list[dict]:
    row = {"source_pdf": pdf_name}
    try:
        text_items = web_app.source_page_text_items(job_dir, row, page_idx)
        layout = web_app.source_page_layout_for_row(job_dir, row, page_idx)
        page_width = float(layout["width_px"])
        page_height = float(layout["height_px"])
    except Exception:
        return []

    if not markers:
        return []

    # Exclude header and footer elements
    text_items = filter_non_body_items(text_items, page_width, page_height, page_idx)

    # 2 columns split threshold
    mid_x = page_width / 2
    for m in markers:
        m["col"] = 0 if float(m["x"]) <= mid_x + 40 else 1

    # Sort markers: column first, then y coordinate
    markers.sort(key=lambda m: (m["col"], float(m["y"])))

    questions_list = []
    for i, m in enumerate(markers):
        col = m["col"]
        start_y = float(m["y"]) - 25.0
        start_x = float(m["x"]) - 10.0
        
        # Bottom limit based on the next marker in the same column
        end_y = page_height
        for next_m in markers[i+1:]:
            if next_m["col"] == col:
                end_y = float(next_m["y"]) - 6.0
                break
        
        # Bounding limits
        min_x = 0.0 if col == 0 else mid_x - 10
        max_x = mid_x + 10 if col == 0 else page_width
        
        belongs_to_q = []
        for item in text_items:
            try:
                left = float(item["left"])
                top = float(item["top"])
                right = float(item["right"])
                bottom = float(item["bottom"])
            except (KeyError, ValueError):
                continue
                
            if top >= start_y and bottom <= end_y + 10:
                item_mid_x = (left + right) / 2
                if min_x <= item_mid_x <= max_x:
                    belongs_to_q.append(item)
                    
        if not belongs_to_q:
            l = start_x
            t = start_y
            r = max_x - 20
            b = min(page_height, start_y + 150)
        else:
            l = min(float(it["left"]) for it in belongs_to_q)
            t = min(float(it["top"]) for it in belongs_to_q)
            r = max(float(it["right"]) for it in belongs_to_q)
            b = max(float(it["bottom"]) for it in belongs_to_q)

        pad_h = 8.0
        pad_v = 6.0
        
        q_entry = {
            "test_no": test_no,
            "test_name": test_name,
            "soru_no": i + 1,
            "page_idx": page_idx,
            "crop_left": max(0.0, l - pad_h),
            "crop_top": max(0.0, t - pad_v),
            "crop_right": min(page_width, r + pad_h),
            "crop_bottom": min(page_height, b + pad_v),
            "anchor_right": None,
            "common_stem": None,
            "sub_crops": []
        }
        
        q_tmp = dict(q_entry)
        q_entry["anchor_right"] = find_question_anchor_right(job_dir, pdf_name, q_tmp)
        
        questions_list.append(q_entry)
        
    return questions_list


def detect_questions_from_xml(
    job_dir: Path,
    pdf_name: str,
    page_idx: int,
    test_no: int = 1,
    test_name: str = "Test-1"
) -> list[dict]:
    row = {"source_pdf": pdf_name}
    try:
        text_items = web_app.source_page_text_items(job_dir, row, page_idx)
        layout = web_app.source_page_layout_for_row(job_dir, row, page_idx)
        page_width = float(layout["width_px"])
        page_height = float(layout["height_px"])
    except Exception:
        return []

    # Exclude header and footer elements
    text_items = filter_non_body_items(text_items, page_width, page_height, page_idx)

    import re
    num_pattern = re.compile(r"^\s*([1-9][0-9]*)\s*[\.\)]")
    
    detected_numbers = []
    for item in text_items:
        text = str(item.get("text") or "").strip()
        match = num_pattern.match(text)
        if match:
            try:
                val = int(match.group(1))
                detected_numbers.append({
                    "number": val,
                    "item": item,
                    "left": float(item["left"]),
                    "top": float(item["top"]),
                    "right": float(item["right"]),
                    "bottom": float(item["bottom"])
                })
            except (ValueError, KeyError):
                continue
                
    if not detected_numbers:
        mid = page_width / 2
        left_items = [it for it in text_items if float(it.get("right", 0)) <= mid + 20]
        right_items = [it for it in text_items if float(it.get("left", 0)) >= mid - 20]
        
        default_qs = []
        if left_items:
            l = min(float(it["left"]) for it in left_items)
            t = min(float(it["top"]) for it in left_items)
            r = max(float(it["right"]) for it in left_items)
            b = max(float(it["bottom"]) for it in left_items)
            default_qs.append({
                "test_no": test_no,
                "test_name": test_name,
                "soru_no": 1,
                "page_idx": page_idx,
                "crop_left": max(0.0, l - 10),
                "crop_top": max(0.0, t - 10),
                "crop_right": min(page_width, r + 10),
                "crop_bottom": min(page_height, b + 10),
                "anchor_right": None,
                "common_stem": None
            })
        if right_items:
            l = min(float(it["left"]) for it in right_items)
            t = min(float(it["top"]) for it in right_items)
            r = max(float(it["right"]) for it in right_items)
            b = max(float(it["bottom"]) for it in right_items)
            default_qs.append({
                "test_no": test_no,
                "test_name": test_name,
                "soru_no": 2,
                "page_idx": page_idx,
                "crop_left": max(0.0, l - 10),
                "crop_top": max(0.0, t - 10),
                "crop_right": min(page_width, r + 10),
                "crop_bottom": min(page_height, b + 10),
                "anchor_right": None,
                "common_stem": None
            })
        return default_qs

    mid_x = page_width / 2
    for num in detected_numbers:
        num["col"] = 0 if num["right"] <= mid_x + 40 else 1

    detected_numbers.sort(key=lambda x: (x["col"], x["top"]))
    
    questions_list = []
    for i, num in enumerate(detected_numbers):
        s_no = i + 1
        col = num["col"]
        start_y = num["top"] - 5.0
        
        end_y = page_height
        for next_num in detected_numbers[i+1:]:
            if next_num["col"] == col:
                end_y = next_num["top"] - 5.0
                break
                
        min_x = 0.0 if col == 0 else mid_x - 10
        max_x = mid_x + 10 if col == 0 else page_width
        
        belongs_to_q = []
        for item in text_items:
            try:
                left = float(item["left"])
                top = float(item["top"])
                right = float(item["right"])
                bottom = float(item["bottom"])
            except (KeyError, ValueError):
                continue
                
            if top >= start_y and bottom <= end_y + 10:
                item_mid_x = (left + right) / 2
                if min_x <= item_mid_x <= max_x:
                    belongs_to_q.append(item)
                    
        if not belongs_to_q:
            belongs_to_q = [num["item"]]
            
        l = min(float(it["left"]) for it in belongs_to_q)
        t = min(float(it["top"]) for it in belongs_to_q)
        r = max(float(it["right"]) for it in belongs_to_q)
        b = max(float(it["bottom"]) for it in belongs_to_q)
        
        pad_h = 8.0
        pad_v = 6.0
        
        q_entry = {
            "test_no": test_no,
            "test_name": test_name,
            "soru_no": s_no,
            "page_idx": page_idx,
            "crop_left": max(0.0, l - pad_h),
            "crop_top": max(0.0, t - pad_v),
            "crop_right": min(page_width, r + pad_h),
            "crop_bottom": min(page_height, b + pad_v),
            "anchor_right": None,
            "common_stem": None
        }
        
        q_tmp = dict(q_entry)
        q_tmp["soru_no"] = num["number"]
        q_entry["anchor_right"] = find_question_anchor_right(job_dir, pdf_name, q_tmp)
        
        questions_list.append(q_entry)
        
    return questions_list


def optimize_crop_bounds(
    job_dir: Path,
    pdf_name: str,
    page_idx: int,
    bounds: dict[str, float],
    soru_no: int = 0,
    hide_question_number: bool = False
) -> dict[str, float]:
    row = {"source_pdf": pdf_name}
    try:
        text_items = web_app.source_page_text_items(job_dir, row, page_idx)
        layout = web_app.source_page_layout_for_row(job_dir, row, page_idx)
        page_width = float(layout["width_px"])
        page_height = float(layout["height_px"])
        text_items = filter_non_body_items(text_items, page_width, page_height, page_idx)
    except Exception:
        return bounds
        
    c_left = bounds["crop_left"]
    c_top = bounds["crop_top"]
    c_right = bounds["crop_right"]
    c_bottom = bounds["crop_bottom"]
    
    # Exclude anchor and its label parts if hiding numbers
    excluded_items = []
    if hide_question_number and soru_no > 0:
        label_prefixes = (f"{soru_no}.", f"{soru_no} .", f"{soru_no})")
        candidates = []
        for item in text_items:
            text = str(item.get("text") or "").strip()
            if not text.startswith(label_prefixes) and text != str(soru_no):
                continue
            left = float(item.get("left", 0))
            top = float(item.get("top", 0))
            right = float(item.get("right", 0))
            bottom = float(item.get("bottom", 0))
            if right <= c_left or left >= c_right or bottom <= c_top or top >= c_bottom:
                continue
            candidates.append(item)
        if candidates:
            anchor_item = min(candidates, key=lambda candidate: (abs(float(candidate.get("top", 0)) - c_top), float(candidate.get("left", 0))))
            excluded_items.append(anchor_item)
            anchor_right = float(anchor_item.get("right", 0))
            anchor_top = float(anchor_item.get("top", 0))
            for other in text_items:
                if other is anchor_item:
                    continue
                other_text = str(other.get("text") or "").strip()
                if not other_text:
                    continue
                is_label_part = False
                if other_text in {".", ")", "-", "a", "b", "c", "d"} or other_text.isdigit():
                    other_left = float(other.get("left", 0))
                    other_top = float(other.get("top", 0))
                    dist_x = abs(other_left - anchor_right)
                    dist_y = abs(other_top - anchor_top)
                    if dist_x < 20.0 and dist_y < 10.0:
                        is_label_part = True
                if is_label_part or other_text.startswith(label_prefixes) or other_text == str(soru_no):
                    excluded_items.append(other)

    inside_items = []
    for item in text_items:
        if item in excluded_items:
            continue
        try:
            left = float(item["left"])
            top = float(item["top"])
            right = float(item["right"])
            bottom = float(item["bottom"])
            text = str(item["text"]).strip()
        except (TypeError, ValueError, KeyError):
            continue
            
        if not text:
            continue
            
        # Tolerans ile çakışma kontrolü (daraltıldı, sadece kutunun içindeki metinler)
        if right < c_left + 2.0 or left > c_right - 2.0:
            continue
        if bottom < c_top + 2.0 or top > c_bottom - 2.0:
            continue
            
        inside_items.append(item)
        
    if not inside_items:
        return bounds
        
    new_left = min(float(item["left"]) for item in inside_items)
    new_top = min(float(item["top"]) for item in inside_items)
    new_right = max(float(item["right"]) for item in inside_items)
    new_bottom = max(float(item["bottom"]) for item in inside_items)
    
    pad_h = 4.0
    pad_v = 3.0
    
    return {
        "crop_left": max(0.0, new_left - pad_h),
        "crop_top": max(0.0, new_top - pad_v),
        "crop_right": new_right + pad_h,
        "crop_bottom": new_bottom + pad_v
    }

def expand_crop_bounds(
    job_dir: Path,
    pdf_name: str,
    page_idx: int,
    bounds: dict[str, float],
    soru_no: int = 0,
    hide_question_number: bool = False
) -> dict[str, float]:
    row = {"source_pdf": pdf_name}
    try:
        text_items = web_app.source_page_text_items(job_dir, row, page_idx)
        layout = web_app.source_page_layout_for_row(job_dir, row, page_idx)
        page_width = float(layout["width_px"])
        page_height = float(layout["height_px"])
        text_items = filter_non_body_items(text_items, page_width, page_height, page_idx)
    except Exception:
        return bounds
        
    c_left = bounds["crop_left"]
    c_top = bounds["crop_top"]
    c_right = bounds["crop_right"]
    c_bottom = bounds["crop_bottom"]
    
    # Exclude anchor and its label parts if hiding numbers
    excluded_items = []
    if hide_question_number and soru_no > 0:
        label_prefixes = (f"{soru_no}.", f"{soru_no} .", f"{soru_no})")
        candidates = []
        for item in text_items:
            text = str(item.get("text") or "").strip()
            if not text.startswith(label_prefixes) and text != str(soru_no):
                continue
            left = float(item.get("left", 0))
            top = float(item.get("top", 0))
            right = float(item.get("right", 0))
            bottom = float(item.get("bottom", 0))
            if right <= c_left or left >= c_right or bottom <= c_top or top >= c_bottom:
                continue
            candidates.append(item)
        if candidates:
            anchor_item = min(candidates, key=lambda candidate: (abs(float(candidate.get("top", 0)) - c_top), float(candidate.get("left", 0))))
            excluded_items.append(anchor_item)
            anchor_right = float(anchor_item.get("right", 0))
            anchor_top = float(anchor_item.get("top", 0))
            for other in text_items:
                if other is anchor_item:
                    continue
                other_text = str(other.get("text") or "").strip()
                if not other_text:
                    continue
                is_label_part = False
                if other_text in {".", ")", "-", "a", "b", "c", "d"} or other_text.isdigit():
                    other_left = float(other.get("left", 0))
                    other_top = float(other.get("top", 0))
                    dist_x = abs(other_left - anchor_right)
                    dist_y = abs(other_top - anchor_top)
                    if dist_x < 20.0 and dist_y < 10.0:
                        is_label_part = True
                if is_label_part or other_text.startswith(label_prefixes) or other_text == str(soru_no):
                    excluded_items.append(other)

    inside_items = []
    for item in text_items:
        if item in excluded_items:
            continue
        try:
            left = float(item["left"])
            top = float(item["top"])
            right = float(item["right"])
            bottom = float(item["bottom"])
            text = str(item["text"]).strip()
        except (TypeError, ValueError, KeyError):
            continue
            
        if not text:
            continue
            
        # Tolerans ile mevcut kutunun içindeki metinler
        if right >= c_left - 2.0 and left <= c_right + 2.0 and bottom >= c_top - 2.0 and top <= c_bottom + 2.0:
            inside_items.append(item)
            
    # Genişletilmiş sınırlar (örneğin 30px uzağındaki komşuları da katalım)
    extended_left = c_left - 30.0
    extended_top = c_top - 30.0
    extended_right = c_right + 30.0
    extended_bottom = c_bottom + 30.0
    
    neighbor_items = []
    for item in text_items:
        if item in inside_items or item in excluded_items:
            continue
        try:
            left = float(item["left"])
            top = float(item["top"])
            right = float(item["right"])
            bottom = float(item["bottom"])
            text = str(item["text"]).strip()
        except (TypeError, ValueError, KeyError):
            continue
            
        if not text:
            continue
            
        # Genişletilmiş sınırlar içerisine düşüyor mu?
        if right >= extended_left and left <= extended_right and bottom >= extended_top and top <= extended_bottom:
            neighbor_items.append(item)
            
    all_target_items = inside_items + neighbor_items
    if not all_target_items:
        # Eğer hiç metin yoksa, standart olarak 20px genişletelim
        return {
            "crop_left": max(0.0, c_left - 20.0),
            "crop_top": max(0.0, c_top - 20.0),
            "crop_right": c_right + 20.0,
            "crop_bottom": c_bottom + 20.0
        }
        
    new_left = min(float(item["left"]) for item in all_target_items)
    new_top = min(float(item["top"]) for item in all_target_items)
    new_right = max(float(item["right"]) for item in all_target_items)
    new_bottom = max(float(item["bottom"]) for item in all_target_items)
    
    pad_h = 4.0
    pad_v = 3.0
    
    return {
        "crop_left": max(0.0, new_left - pad_h),
        "crop_top": max(0.0, new_top - pad_v),
        "crop_right": new_right + pad_h,
        "crop_bottom": new_bottom + pad_v
    }

def find_question_anchor_right(job_dir: Path, pdf_name: str, q: dict) -> float | None:
    try:
        soru_no = int(q.get("soru_no") or 0)
        if soru_no <= 0:
            return None
        page_idx = int(q.get("page_idx") or 0)
        crop_left = float(q.get("crop_left") or 0.0)
        crop_top = float(q.get("crop_top") or 0.0)
        crop_right = float(q.get("crop_right") or 0.0)
        crop_bottom = float(q.get("crop_bottom") or 0.0)
        label_prefixes = (f"{soru_no}.", f"{soru_no} .", f"{soru_no})")
        
        row = {"source_pdf": pdf_name}
        text_items = web_app.source_page_text_items(job_dir, row, page_idx)
        candidates = []
        for item in text_items:
            text = str(item["text"]).strip()
            if not text.startswith(label_prefixes) and text != str(soru_no):
                continue
            left = float(item["left"])
            top = float(item["top"])
            right = float(item["right"])
            bottom = float(item["bottom"])
            if right <= crop_left or left >= crop_right or bottom <= crop_top or top >= crop_bottom:
                continue
            candidates.append(item)
            
        if not candidates:
            return None
            
        item = min(candidates, key=lambda candidate: (abs(float(candidate["top"]) - crop_top), float(candidate["left"])))
        item_left = float(item["left"])
        item_right = float(item["right"])
        item_width = item_right - item_left
        item_height = max(8.0, float(item["height"]))
        
        # Sondaki boşlukları hesaba katarak genişliği orantılı olarak küçült
        raw_text = str(item["text"])
        stripped_text = raw_text.rstrip()
        if len(raw_text) > 0 and len(stripped_text) < len(raw_text):
            item_width = item_width * (len(stripped_text) / len(raw_text))
            item_right = item_left + item_width

        text = raw_text.strip()
        if text.startswith(label_prefixes):
            import re
            is_pure_label = bool(re.match(r"^[0-9.\s()]+$", text))
            if not is_pure_label:
                digit_count = len(str(soru_no))
                item_right = min(item_right, item_left + item_height * (0.50 + digit_count * 0.30))
        
        pad = 1.5
        new_left = item_right + pad
        if new_left >= crop_right - 20:
            return None

        # Soru metninin kesilmemesi için çakışma (overlap) kontrolü
        for other in text_items:
            if other is item:
                continue
            other_left = float(other["left"])
            other_top = float(other["top"])
            other_right = float(other["right"])
            other_bottom = float(other["bottom"])
            if other_right <= crop_left or other_left >= crop_right:
                continue
            if other_bottom <= crop_top or other_top >= crop_bottom:
                continue
            other_text = str(other["text"]).strip()
            if not other_text:
                continue
            
            # Soru etiketinin parçası olup olmadığını kontrol et (nokta, parantez vb.)
            is_label_part = False
            if other_text in {".", ")", "-", "a", "b", "c", "d"} or other_text.isdigit():
                dist_x = abs(other_left - item_right)
                dist_y = abs(other_top - float(item["top"]))
                if dist_x < 20.0 and dist_y < 10.0:
                    is_label_part = True
            if is_label_part or other_text.startswith(label_prefixes) or other_text == str(soru_no):
                continue
            
            # Eğer başka bir metin yeni sol sınırın solunda kalıyorsa, kaydırma yapma!
            if other_left < new_left - 2.0:
                return None

        return new_left
    except Exception as exc:
        print(f"[easy_editor] find_question_anchor_right error: {exc}")
        return None

def get_pdf_pages_dimensions(job_dir: Path, source_pdf: str, pdf_path: Path) -> list[dict[str, float]]:
    """Return pdftohtml-compatible page dimensions without invoking Poppler.

    pdftohtml's default coordinate scale is 1.5. Running pdftohtml once per
    page here made the editor navigation wait for the entire PDF to be parsed
    before it could render.
    """
    reader = PdfReader(pdf_path)
    dimensions = []
    for page in reader.pages:
        box = page.mediabox
        width = float(box.width) * 1.5
        height = float(box.height) * 1.5
        rotation = int(page.get("/Rotate", 0) or 0) % 360
        if rotation in {90, 270}:
            width, height = height, width
        dimensions.append({"width": width, "height": height})
    return dimensions

_render_lock = threading.Lock()

def get_pdf_page_image(job_id: str, pdf_name: str, page_idx: int, dpi: int = 150) -> Path:
    job_dir, _ = web_app.get_active_job(job_id)
    pdf_path = job_dir / "input" / pdf_name
    page_no = int(page_idx) + 1
    cache_dir = job_dir / "preview_cache"
    web_app.ensure_dir(cache_dir)
    
    # Reuse images produced by the previous all-pages renderer, but never
    # trigger that expensive path for a cache miss.
    pre_rendered_path = cache_dir / f"easy_page_{pdf_name}_all_{dpi}-{page_no}.png"
    if pre_rendered_path.exists():
        return pre_rendered_path

    target_image_path = cache_dir / f"easy_page_{pdf_name}_{page_no}_{dpi}.png"
    if target_image_path.exists():
        return target_image_path

    # Synchronize rendering using global lock
    with _render_lock:
        # Check again under lock
        if target_image_path.exists():
            return target_image_path
            
        if not target_image_path.exists():
            out_prefix = cache_dir / f"easy_page_{pdf_name}_{page_no}_{dpi}"
            result = web_app.subprocess.run(
                [
                    get_binary_path("pdftocairo"),
                    "-png",
                    "-singlefile",
                    "-r",
                    str(dpi),
                    "-f",
                    str(page_no),
                    "-l",
                    str(page_no),
                    str(pdf_path),
                    str(out_prefix),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=web_app.processor_env(),
            )
            if result.returncode != 0:
                print(f"[get_pdf_page_image] pdftocairo failed: {result.stderr}")
                
    if not target_image_path.exists():
        raise HTTPException(status_code=500, detail="Sayfa görseli üretilemedi")
        
    return target_image_path

def register_easy_editor(app: FastAPI) -> None:
    @app.get("/jobs/{job_id}/easy-editor/page-image")
    def easy_editor_page_image(job_id: str, pdf_name: str, page_idx: int) -> FileResponse:
        if not web_app.FEEDBACK_MODE:
            raise HTTPException(status_code=404, detail="Önizleme kapalı")
        image_path = get_pdf_page_image(job_id, pdf_name, page_idx, dpi=150)
        return FileResponse(image_path, media_type="image/png")

    @app.get("/jobs/{job_id}/easy-editor", response_class=HTMLResponse)
    def job_easy_editor(job_id: str, pdf_name: str | None = None) -> HTMLResponse:
        if not web_app.FEEDBACK_MODE:
            raise HTTPException(status_code=404, detail="Kolay düzenleme kapalı")
        try:
            job_dir, meta = web_app.get_active_job(job_id)
        except HTTPException as exc:
            if exc.status_code in {404, 410}:
                return web_app.expired_page()
            raise

        out_dir = job_dir / "output"
        manifest_paths = list(out_dir.glob("*_crop_manifest.json"))
        
        manifest = []
        if manifest_paths:
            manifest_path = manifest_paths[0]
            if pdf_name:
                for path in manifest_paths:
                    if pdf_name in path.name:
                        manifest_path = path
                        break
            manifest = web_app.read_json_file(manifest_path)
            if not isinstance(manifest, list):
                manifest = []
                
            # Auto backup original manifest
            original_manifest_path = manifest_path.with_name(manifest_path.stem + "_original.json")
            if manifest_path.exists() and not original_manifest_path.exists():
                try:
                    shutil.copy2(manifest_path, original_manifest_path)
                except Exception as exc:
                    print(f"[easy_editor] Failed to back up original manifest: {exc}")

        source_pdf = pdf_name or str(meta.get("pdf_name") or "")
        if not source_pdf and manifest:
            source_pdf = str(manifest[0].get("source_pdf") or "")
        
        pdf_path = job_dir / "input" / source_pdf
        if not pdf_path.exists():
            import unicodedata
            input_dir = job_dir / "input"
            name_nfc = unicodedata.normalize("NFC", source_pdf)
            name_nfd = unicodedata.normalize("NFD", source_pdf)
            if (input_dir / name_nfc).exists():
                pdf_path = input_dir / name_nfc
                source_pdf = name_nfc
            elif (input_dir / name_nfd).exists():
                pdf_path = input_dir / name_nfd
                source_pdf = name_nfd
            else:
                input_pdfs = list(input_dir.glob("*.pdf"))
                if input_pdfs:
                    pdf_path = input_pdfs[0]
                    source_pdf = pdf_path.name
                else:
                    raise HTTPException(status_code=404, detail="Kaynak PDF dosyası bulunamadı")

        dimensions = get_pdf_pages_dimensions(job_dir, source_pdf, pdf_path)

        navigation_tests = []
        try:
            navigation_tests = detect_editor_test_navigation(job_dir, source_pdf, pdf_path, len(dimensions))
        except Exception as exc:
            print(f"[easy_editor] Test navigation detection failed: {exc}")

        questions = []
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("content_type") or "question") != "question":
                continue
            
            common_stem = None
            if entry.get("common_stem_left") is not None:
                common_stem = {
                    "page_idx": entry.get("common_stem_page_idx"),
                    "crop_left": float(entry.get("common_stem_left")),
                    "crop_top": float(entry.get("common_stem_top")),
                    "crop_right": float(entry.get("common_stem_right")),
                    "crop_bottom": float(entry.get("common_stem_bottom")),
                    "placement": entry.get("common_stem_placement") or "top"
                }

            q_dict = {
                "test_no": int(entry.get("test_no") or 1) or 1,
                "test_name": str(entry.get("test_name") or ""),
                "soru_no": int(entry.get("soru_no") or 0),
                "page_idx": int(entry.get("page_idx") or 0),
                "crop_left": float(entry.get("crop_left") or 0.0),
                "crop_top": float(entry.get("crop_top") or 0.0),
                "crop_right": float(entry.get("crop_right") or 0.0),
                "crop_bottom": float(entry.get("crop_bottom") or 0.0),
                "common_stem": common_stem
            }
            anchor_right = entry.get("anchor_right")
            if anchor_right is None:
                anchor_right = find_question_anchor_right(job_dir, source_pdf, q_dict)
            else:
                anchor_right = float(anchor_right)
            q_dict["anchor_right"] = anchor_right
            questions.append(q_dict)

        tests_map = {}
        for q in questions:
            t_name = q["test_name"]
            import re
            m = re.match(r"^Test[_\s-]*0*([1-9][0-9]*)(.*)$", t_name, re.IGNORECASE)
            if m:
                t_no = int(m.group(1))
                t_name = f"Test-{t_no}"
                q["test_name"] = t_name
            tests_map[q["test_no"]] = t_name
        
        if not tests_map:
            tests_map[1] = "Test-1"
        tests = [{"test_no": k, "test_name": v} for k, v in sorted(tests_map.items())]

        if not navigation_tests:
            for test in tests:
                page_indices = [q["page_idx"] for q in questions if q["test_no"] == test["test_no"]]
                if not page_indices:
                    continue
                navigation_tests.append({
                    "test_no": test["test_no"],
                    "label": test["test_name"],
                    "start_page_idx": min(page_indices),
                    "end_page_idx": max(page_indices),
                })

        hide_numbers_by_default = bool(meta.get("hide_question_number", False))
        if manifest:
            has_hidden_key = any("question_number_hidden" in entry for entry in manifest if isinstance(entry, dict))
            if has_hidden_key:
                hide_numbers_by_default = any(entry.get("question_number_hidden") for entry in manifest if isinstance(entry, dict))

        selected_pages_str = meta.get("selected_pages")
        total_pages = len(dimensions)
        selected_indices = parse_selected_page_indices(selected_pages_str, total_pages)

        editor_data = {
            "job_id": job_id,
            "pdf_name": source_pdf,
            "status": meta.get("status", "queued"),
            "workflow_mode": meta.get("workflow_mode", "automatic"),
            "hide_question_numbers": hide_numbers_by_default,
            "pages": [
                {
                    "page_idx": idx,
                    "width": dim["width"],
                    "height": dim["height"],
                    "image_url": f"/jobs/{job_id}/easy-editor/page-image?pdf_name={quote(source_pdf)}&page_idx={idx}"
                }
                for idx, dim in enumerate(dimensions)
                if selected_indices is None or idx in selected_indices
            ],
            "questions": questions,
            "tests": tests,
            "navigation_tests": navigation_tests,
        }

        return HTMLResponse(easy_editor_html_page(editor_data))

    @app.get("/jobs/{job_id}/easy-editor/questions-json")
    def easy_editor_questions_json(job_id: str) -> JSONResponse:
        job_dir, meta = web_app.get_active_job(job_id)
        out_dir = job_dir / "output"
        manifest_paths = list(out_dir.glob("*_crop_manifest.json"))
        if not manifest_paths:
            return JSONResponse({"questions": []})
        manifest = web_app.read_json_file(manifest_paths[0])
        if not isinstance(manifest, list):
            return JSONResponse({"questions": []})
        
        pdf_name = meta.get("pdf_name") or ""
        questions = []
        for entry in manifest:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("content_type") or "question") != "question":
                continue
            
            common_stem = None
            if entry.get("common_stem_left") is not None:
                common_stem = {
                    "page_idx": entry.get("common_stem_page_idx"),
                    "crop_left": float(entry.get("common_stem_left")),
                    "crop_top": float(entry.get("common_stem_top")),
                    "crop_right": float(entry.get("common_stem_right")),
                    "crop_bottom": float(entry.get("common_stem_bottom")),
                    "placement": entry.get("common_stem_placement") or "top"
                }

            t_name = str(entry.get("test_name") or "")
            import re
            m = re.match(r"^Test[_\s-]*0*([1-9][0-9]*)(.*)$", t_name, re.IGNORECASE)
            if m:
                t_no = int(m.group(1))
                t_name = f"Test-{t_no}"

            q_dict = {
                "test_no": int(entry.get("test_no") or 1) or 1,
                "test_name": t_name,
                "soru_no": int(entry.get("soru_no") or 0),
                "page_idx": int(entry.get("page_idx") or 0),
                "crop_left": float(entry.get("crop_left") or 0.0),
                "crop_top": float(entry.get("crop_top") or 0.0),
                "crop_right": float(entry.get("crop_right") or 0.0),
                "crop_bottom": float(entry.get("crop_bottom") or 0.0),
                "common_stem": common_stem,
                "sub_crops": entry.get("sub_crops") or []
            }
            anchor_right = entry.get("anchor_right")
            if anchor_right is None:
                anchor_right = find_question_anchor_right(job_dir, pdf_name, q_dict)
            else:
                anchor_right = float(anchor_right)
            q_dict["anchor_right"] = anchor_right
            questions.append(q_dict)
        return JSONResponse({"questions": questions})

    @app.get("/jobs/{job_id}/easy-editor/original-questions")
    def easy_editor_original_questions(
        job_id: str,
        page_idx: int,
        force_reanalyze: bool = False,
        test_no: int | None = None,
        test_name: str | None = None
    ) -> JSONResponse:
        job_dir, meta = web_app.get_active_job(job_id)
        pdf_name = meta.get("pdf_name") or ""
        out_dir = job_dir / "output"
        
        t_no = test_no if test_no is not None else 1
        t_name = test_name if test_name is not None else "Test-1"
        
        questions = []
        if not force_reanalyze:
            manifest = []
            manifest_paths = list(out_dir.glob("*_crop_manifest_original.json"))
            if not manifest_paths:
                manifest_paths = list(out_dir.glob("*_crop_manifest.json"))
            
            if manifest_paths:
                manifest = web_app.read_json_file(manifest_paths[0])
                if not isinstance(manifest, list):
                    manifest = []
            
            for entry in manifest:
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("content_type") or "question") != "question":
                    continue
                if int(entry.get("page_idx") or 0) != page_idx:
                    continue
                
                common_stem = None
                if entry.get("common_stem_left") is not None:
                    common_stem = {
                        "page_idx": entry.get("common_stem_page_idx"),
                        "crop_left": float(entry.get("common_stem_left")),
                        "crop_top": float(entry.get("common_stem_top")),
                        "crop_right": float(entry.get("common_stem_right")),
                        "crop_bottom": float(entry.get("common_stem_bottom")),
                        "placement": entry.get("common_stem_placement") or "top"
                    }

                anchor_right = entry.get("anchor_right")
                q_pdf = str(entry.get("source_pdf") or "")
                if anchor_right is None:
                    q_tmp = {
                        "soru_no": int(entry.get("soru_no") or 0),
                        "page_idx": int(entry.get("page_idx") or 0),
                        "crop_left": float(entry.get("crop_left") or 0.0),
                        "crop_top": float(entry.get("crop_top") or 0.0),
                        "crop_right": float(entry.get("crop_right") or 0.0),
                        "crop_bottom": float(entry.get("crop_bottom") or 0.0)
                    }
                    anchor_right = find_question_anchor_right(job_dir, q_pdf, q_tmp)
                else:
                    anchor_right = float(anchor_right)

                questions.append({
                    "test_no": int(entry.get("test_no") or 0),
                    "test_name": str(entry.get("test_name") or ""),
                    "soru_no": int(entry.get("soru_no") or 0),
                    "page_idx": int(entry.get("page_idx") or 0),
                    "crop_left": float(entry.get("crop_left") or 0.0),
                    "crop_top": float(entry.get("crop_top") or 0.0),
                    "crop_right": float(entry.get("crop_right") or 0.0),
                    "crop_bottom": float(entry.get("crop_bottom") or 0.0),
                    "anchor_right": anchor_right,
                    "common_stem": common_stem,
                    "sub_crops": entry.get("sub_crops") or []
                })
            
        if not questions:
            questions = detect_questions_from_xml(job_dir, pdf_name, page_idx, test_no=t_no, test_name=t_name)
            
        return JSONResponse({"questions": questions})

    @app.post("/jobs/{job_id}/easy-editor/detect-from-markers")
    def easy_editor_detect_from_markers(job_id: str, payload: dict) -> JSONResponse:
        job_dir, meta = web_app.get_active_job(job_id)
        pdf_name = meta.get("pdf_name") or ""
        page_idx = int(payload["page_idx"])
        markers = list(payload.get("markers") or [])
        test_no = int(payload.get("test_no") or 1)
        test_name = str(payload.get("test_name") or "Test-1")
        
        detected = detect_questions_from_markers(job_dir, pdf_name, page_idx, markers, test_no=test_no, test_name=test_name)
        return JSONResponse({"questions": detected})

    @app.post("/jobs/{job_id}/easy-editor/optimize-box")
    def easy_editor_optimize_box(job_id: str, payload: dict) -> JSONResponse:
        job_dir, meta = web_app.get_active_job(job_id)
        pdf_name = meta.get("pdf_name") or ""
        page_idx = int(payload["page_idx"])
        bounds = {
            "crop_left": float(payload["crop_left"]),
            "crop_top": float(payload["crop_top"]),
            "crop_right": float(payload["crop_right"]),
            "crop_bottom": float(payload["crop_bottom"])
        }
        soru_no = int(payload.get("soru_no") or 0)
        
        optimized = optimize_crop_bounds(job_dir, pdf_name, page_idx, bounds)
        
        # anchor_right'ı da tekrar hesapla
        if soru_no > 0:
            q_tmp = dict(optimized)
            q_tmp["soru_no"] = soru_no
            q_tmp["page_idx"] = page_idx
            optimized["anchor_right"] = find_question_anchor_right(job_dir, pdf_name, q_tmp)
        else:
            optimized["anchor_right"] = None
            
        return JSONResponse(optimized)

    @app.post("/jobs/{job_id}/easy-editor/adjust-box")
    def easy_editor_adjust_box(job_id: str, payload: dict) -> JSONResponse:
        job_dir, meta = web_app.get_active_job(job_id)
        pdf_name = meta.get("pdf_name") or ""
        page_idx = int(payload["page_idx"])
        action = str(payload.get("action") or "expand")
        bounds = {
            "crop_left": float(payload["crop_left"]),
            "crop_top": float(payload["crop_top"]),
            "crop_right": float(payload["crop_right"]),
            "crop_bottom": float(payload["crop_bottom"])
        }
        soru_no = int(payload.get("soru_no") or 0)
        hide_question_number = bool(payload.get("hide_question_number", False))
        
        if action == "shrink":
            adjusted = optimize_crop_bounds(job_dir, pdf_name, page_idx, bounds, soru_no=soru_no, hide_question_number=hide_question_number)
        else:
            adjusted = expand_crop_bounds(job_dir, pdf_name, page_idx, bounds, soru_no=soru_no, hide_question_number=hide_question_number)
            
        if soru_no > 0:
            q_tmp = dict(adjusted)
            q_tmp["soru_no"] = soru_no
            q_tmp["page_idx"] = page_idx
            adjusted["anchor_right"] = find_question_anchor_right(job_dir, pdf_name, q_tmp)
        else:
            adjusted["anchor_right"] = None
            
        return JSONResponse(adjusted)

    @app.post("/jobs/{job_id}/easy-editor/save")
    def save_easy_editor(job_id: str, payload: dict) -> JSONResponse:
        if not web_app.FEEDBACK_MODE:
            raise HTTPException(status_code=404, detail="Düzenleme kapalı")
        job_dir, meta = web_app.get_active_job(job_id)
        out_dir = job_dir / "output"

        pdf_name = str(payload.get("pdf_name") or meta.get("pdf_name") or "")
        if not pdf_name:
            raise HTTPException(status_code=400, detail="PDF adı eksik")

        manifest_path = out_dir / f"{Path(pdf_name).stem}_crop_manifest.json"
        if not manifest_path.exists():
            manifest_paths = list(out_dir.glob("*_crop_manifest.json"))
            if manifest_paths:
                manifest_path = manifest_paths[0]
            else:
                raise HTTPException(status_code=404, detail="Manifest bulunamadı")

        profiles = web_app.load_learning_profiles(out_dir)
        profile = profiles.get(pdf_name) or {}
        profile_key = str(profile.get("profile_key") or "")

        # Backup manifest if original backup doesn't exist
        original_manifest_path = manifest_path.with_name(manifest_path.stem + "_original.json")
        if manifest_path.exists() and not original_manifest_path.exists():
            try:
                shutil.copy2(manifest_path, original_manifest_path)
            except Exception as exc:
                print(f"[easy_editor] Failed to back up original manifest: {exc}")

        # Read original manifest for learning/drawing before writing new manifest
        old_manifest = []
        if original_manifest_path.exists():
            old_manifest = web_app.read_json_file(original_manifest_path)
        else:
            old_manifest = web_app.read_json_file(manifest_path)

        subfolder = web_app.local_sanitize(Path(pdf_name).stem)

        # Clean the main subfolder completely to avoid any leftover/deleted test PDFs or question PDFs
        main_subfolder_dir = out_dir / subfolder
        if main_subfolder_dir.exists():
            try:
                shutil.rmtree(main_subfolder_dir, ignore_errors=True)
            except Exception as exc:
                print(f"[easy_editor] Failed to clean main subfolder: {exc}")
        web_app.ensure_dir(main_subfolder_dir)

        if isinstance(old_manifest, list):
            # Clean old test output folders/files
            old_test_names = {str(item.get("test_name") or "") for item in old_manifest if isinstance(item, dict) and item.get("test_name")}
            for name in old_test_names:
                if name:
                    # Resolve both raw and normalized names to ensure full cleanup
                    m = re.match(r"^Test[_\s-]*0*([1-9][0-9]*)(.*)$", name, re.IGNORECASE)
                    norm_names = {name}
                    if m:
                        t_no = int(m.group(1))
                        norm_names.add(f"Test-{t_no}")

                    for clean_name in norm_names:
                        # Clean stray/duplicate files directly in out_dir (from old versions)
                        stray_q_dir = out_dir / f"{web_app.local_sanitize(clean_name)}_questions"
                        if stray_q_dir.exists():
                            shutil.rmtree(stray_q_dir, ignore_errors=True)
                        (out_dir / f"{web_app.local_sanitize(clean_name)}_pages.pdf").unlink(missing_ok=True)

        new_questions = payload.get("questions") or []
        hide_q_nums = bool(payload.get("hide_question_numbers", True))
        modified_pages = payload.get("modified_pages") or []
        new_manifest = []

        db_bounds = {}
        db_stems = {}

        for q in new_questions:
            test_no = int(q["test_no"])
            test_name = str(q["test_name"])
            soru_no = int(q["soru_no"])
            page_idx = int(q["page_idx"])
            crop_left = float(q["crop_left"])
            crop_top = float(q["crop_top"])
            crop_right = float(q["crop_right"])
            crop_bottom = float(q["crop_bottom"])
            anchor_right = q.get("anchor_right")
            if anchor_right is not None:
                anchor_right = float(anchor_right)

            common_stem = q.get("common_stem")
            c_page_idx = None
            c_left = None
            c_top = None
            c_right = None
            c_bottom = None
            c_placement = "top"

            if common_stem:
                c_page_idx = int(common_stem["page_idx"])
                c_left = float(common_stem["crop_left"])
                c_top = float(common_stem["crop_top"])
                c_right = float(common_stem["crop_right"])
                c_bottom = float(common_stem["crop_bottom"])
                c_placement = str(common_stem.get("placement") or "top")

                db_stems[f"{test_no}:{soru_no}"] = {
                    "source_pdf": pdf_name,
                    "test_no": test_no,
                    "test_name": test_name,
                    "source_soru_no": 1,
                    "source_page_idx": c_page_idx,
                    "soru_no": soru_no,
                    "bounds": {
                        "crop_left": c_left,
                        "crop_top": c_top,
                        "crop_right": c_right,
                        "crop_bottom": c_bottom
                    },
                    "placement": c_placement,
                    "updated_at": int(time.time())
                }

            target_rel_path = f"{subfolder}/{web_app.local_sanitize(test_name)}_questions/{soru_no:02d}.pdf"

            entry = {
                "source_pdf": pdf_name,
                "test_no": test_no,
                "test_name": test_name,
                "soru_no": soru_no,
                "page_idx": page_idx,
                "crop_left": crop_left,
                "crop_top": crop_top,
                "crop_right": crop_right,
                "crop_bottom": crop_bottom,
                "anchor_right": anchor_right,
                "method": "manual-easy-editor",
                "question_pdf": target_rel_path,
                "question_number_hidden": hide_q_nums,
                "common_stem_page_idx": c_page_idx,
                "common_stem_left": c_left,
                "common_stem_top": c_top,
                "common_stem_right": c_right,
                "common_stem_bottom": c_bottom,
                "common_stem_placement": c_placement,
                "content_type": "question",
                "sub_crops": q.get("sub_crops") or []
            }
            new_manifest.append(entry)

            db_bounds[f"{test_no}:{soru_no}"] = {
                "source_pdf": pdf_name,
                "test_no": test_no,
                "test_name": test_name,
                "soru_no": soru_no,
                "page_idx": page_idx,
                "bounds": {
                    "crop_left": crop_left,
                    "crop_top": crop_top,
                    "crop_right": crop_right,
                    "crop_bottom": crop_bottom,
                    "anchor_right": anchor_right
                },
                "updated_at": int(time.time())
            }

        # Write manifest file
        manifest_path.write_text(json.dumps(new_manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # Save manual_feedback database profile
        if profile_key:
            feedback_path = Path(__file__).parent / "crop_memory" / "manual_feedback.json"
            if feedback_path.exists():
                try:
                    with open(feedback_path, 'r', encoding='utf-8') as f:
                        db = json.load(f)
                    profiles_db = db.setdefault("profiles", {})
                    prof_data = profiles_db.setdefault(profile_key, {
                        "profile_key": profile_key,
                        "created_at": int(time.time()),
                        "last_feedback_at": 0,
                        "issue_counts": {},
                        "question_issue_counts": {},
                        "question_bounds": {},
                        "question_common_stems": {},
                        "events": []
                    })
                    prof_data["standardize_columns"] = True
                    prof_data.setdefault("question_bounds", {}).update(db_bounds)
                    prof_data.setdefault("question_common_stems", {}).update(db_stems)
                    
                    with open(feedback_path, 'w', encoding='utf-8') as f:
                        json.dump(db, f, ensure_ascii=False, indent=2)
                except Exception as exc:
                    print(f"[EASY EDITOR DB ERROR] {type(exc).__name__}: {exc}")

        # --- LOG MANUAL CORRECTIONS & GENERATE LEARNING DATA ---
        if modified_pages:
            try:
                # 1. Create learning folder
                learning_dir = Path(__file__).parent / "crop_memory" / "learning_data" / job_id
                learning_dir.mkdir(parents=True, exist_ok=True)

                # 2. Extract original vs corrected question logs
                orig_learning_list = []
                corr_learning_list = []
                for entry in old_manifest:
                    if isinstance(entry, dict) and entry.get("page_idx") in modified_pages:
                        orig_learning_list.append(entry)
                for entry in new_manifest:
                    if isinstance(entry, dict) and entry.get("page_idx") in modified_pages:
                        corr_learning_list.append(entry)

                now_timestamp = int(time.time())
                corrections_log = {
                    "job_id": job_id,
                    "pdf_name": pdf_name,
                    "timestamp": now_timestamp,
                    "modified_pages": modified_pages,
                    "original_questions": orig_learning_list,
                    "corrected_questions": corr_learning_list
                }
                
                # Write log file
                log_file = learning_dir / "corrections.json"
                log_file.write_text(json.dumps(corrections_log, ensure_ascii=False, indent=2), encoding="utf-8")

                # Get page dimensions
                pdf_path = job_dir / "input" / pdf_name
                dimensions = get_pdf_pages_dimensions(job_dir, pdf_name, pdf_path)

                from PIL import Image, ImageDraw
                for page_idx in modified_pages:
                    page_no = page_idx + 1
                    if page_idx < 0 or page_idx >= len(dimensions):
                        continue
                    page_dim = dimensions[page_idx]

                    # Get page image path
                    image_path = get_pdf_page_image(job_id, pdf_name, page_idx, dpi=150)
                    
                    # Extract boxes for this page
                    # Original boxes on page
                    orig_boxes = []
                    seen_stems = set()
                    for q in orig_learning_list:
                        if q.get("page_idx") == page_idx:
                            orig_boxes.append({
                                "label": f"Soru {q.get('soru_no'):02d}",
                                "crop_left": float(q["crop_left"]),
                                "crop_top": float(q["crop_top"]),
                                "crop_right": float(q["crop_right"]),
                                "crop_bottom": float(q["crop_bottom"]),
                                "type": "question"
                            })
                        if q.get("common_stem_page_idx") == page_idx:
                            c_coords = (
                                float(q["common_stem_left"]),
                                float(q["common_stem_top"]),
                                float(q["common_stem_right"]),
                                float(q["common_stem_bottom"])
                            )
                            if c_coords not in seen_stems:
                                seen_stems.add(c_coords)
                                orig_boxes.append({
                                    "label": "Ortak Kök",
                                    "crop_left": c_coords[0],
                                    "crop_top": c_coords[1],
                                    "crop_right": c_coords[2],
                                    "crop_bottom": c_coords[3],
                                    "type": "stem"
                                })

                    # Corrected boxes on page
                    corr_boxes = []
                    seen_stems = set()
                    for q in corr_learning_list:
                        if q.get("page_idx") == page_idx:
                            corr_boxes.append({
                                "label": f"Soru {q.get('soru_no'):02d}",
                                "crop_left": float(q["crop_left"]),
                                "crop_top": float(q["crop_top"]),
                                "crop_right": float(q["crop_right"]),
                                "crop_bottom": float(q["crop_bottom"]),
                                "type": "question"
                            })
                        if q.get("common_stem_page_idx") == page_idx:
                            c_coords = (
                                float(q["common_stem_left"]),
                                float(q["common_stem_top"]),
                                float(q["common_stem_right"]),
                                float(q["common_stem_bottom"])
                            )
                            if c_coords not in seen_stems:
                                seen_stems.add(c_coords)
                                corr_boxes.append({
                                    "label": "Ortak Kök",
                                    "crop_left": c_coords[0],
                                    "crop_top": c_coords[1],
                                    "crop_right": c_coords[2],
                                    "crop_bottom": c_coords[3],
                                    "type": "stem"
                                })

                    # Helper to draw and save
                    def draw_and_save(boxes_to_draw, dest_filename):
                        img = Image.open(image_path).convert("RGB")
                        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
                        overlay_draw = ImageDraw.Draw(overlay)
                        
                        img_w, img_h = img.size
                        x_scale = img_w / page_dim["width"]
                        y_scale = img_h / page_dim["height"]

                        for box in boxes_to_draw:
                            l = box["crop_left"] * x_scale
                            t = box["crop_top"] * y_scale
                            r = box["crop_right"] * x_scale
                            b = box["crop_bottom"] * y_scale
                            
                            is_stem = box["type"] == "stem"
                            fill_color = (168, 85, 247, 38) if is_stem else (59, 130, 246, 38)
                            border_color = (168, 85, 247, 255) if is_stem else (59, 130, 246, 255)
                            
                            overlay_draw.rectangle([l, t, r, b], fill=fill_color, outline=border_color, width=3)
                            
                            # Draw label
                            label_text = box["label"]
                            tw = len(label_text) * 8
                            tw = tw if tw > 0 else 60
                            th = 15
                            label_top = t - th if t - th >= 0 else t
                            label_bottom = t if t - th >= 0 else t + th
                            overlay_draw.rectangle([l, label_top, l + tw, label_bottom], fill=(15, 23, 42, 240))
                            overlay_draw.text((l + 3, label_top + 1), label_text, fill=(255, 255, 255, 255))
                            
                        out_img = Image.alpha_composite(img.convert("RGBA"), overlay)
                        out_img.convert("RGB").save(learning_dir / dest_filename, "PNG")

                    draw_and_save(orig_boxes, f"page_{page_no}_original.png")
                    draw_and_save(corr_boxes, f"page_{page_no}_corrected.png")

                    # Create archive entry under LOCAL_JOBS_ROOT for admin panel
                    folder_name = f"{now_timestamp}_page{page_no:02d}_easy_editor"
                    archive_dir = web_app.local_job_archive_dir(job_id) / "feedback" / folder_name
                    web_app.ensure_dir(archive_dir)

                    page_orig = [q for q in orig_learning_list if isinstance(q, dict) and q.get("page_idx") == page_idx]
                    page_corr = [q for q in corr_learning_list if isinstance(q, dict) and q.get("page_idx") == page_idx]

                    record_id = f"{now_timestamp}-page{page_no:02d}"
                    report = {
                        "record_id": record_id,
                        "job_id": job_id,
                        "created_at": now_timestamp,
                        "kind": "easy_editor",
                        "issue_code": "easy_editor_correction",
                        "note": "Kolay Düzenleyici ile sayfa düzeltildi",
                        "source_pdf": pdf_name,
                        "test_no": test_no,
                        "test_name": test_name,
                        "soru_no": 0,
                        "page_idx": page_idx,
                        "profile_key": profile_key or "",
                        "question_pdf": "",
                        "false_cut": f"page_{page_no}_original.png",
                        "true_cut": f"page_{page_no}_corrected.png",
                        "page_image": "",
                        "bounds": None,
                        "common_stem": None,
                        "images": [f"page_{page_no}_original.png", f"page_{page_no}_corrected.png"],
                        "original_questions": page_orig,
                        "corrected_questions": page_corr
                    }
                    (archive_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

                    (archive_dir / "report.md").write_text(
                        "\n".join([
                            f"# Sayfa Düzeltme - Sayfa {page_no}",
                            "",
                            f"- Kayıt id: `{record_id}`",
                            f"- İş: `{job_id}`",
                            f"- Kaynak PDF: `{pdf_name}`",
                            f"- Hata nedeni: `Kolay Düzenleyici Düzeltmesi`",
                            f"- Not: Kolay Düzenleyici ile sayfa düzeltildi",
                            f"- Yanlış hali: `page_{page_no}_original.png`",
                            f"- Doğru hali: `page_{page_no}_corrected.png`",
                        ]),
                        encoding="utf-8"
                    )

                    shutil.copy2(learning_dir / f"page_{page_no}_original.png", archive_dir / f"page_{page_no}_original.png")
                    shutil.copy2(learning_dir / f"page_{page_no}_corrected.png", archive_dir / f"page_{page_no}_corrected.png")
            except Exception as exc:
                print(f"[EASY EDITOR LEARNING LOG ERROR] {type(exc).__name__}: {exc}")

        # --- OPTIMIZED CROP AND REBUILD ---
        new_rows = web_app.load_review_rows(out_dir)
        dirs_to_rebuild = set()
        for idx, row in enumerate(new_rows):
            question_pdf = str(row.get("question_pdf") or "")
            if not question_pdf:
                continue
            pdf_path = (out_dir / question_pdf).resolve()
            # Crop the question
            crop = web_app.compose_question_image_from_row(job_id, idx, dpi=450)
            web_app.ensure_dir(pdf_path.parent)
            crop.save(pdf_path, "PDF", resolution=450)
            
            # Clear preview cache
            cache_dir = job_dir / "preview_cache"
            old_preview = cache_dir / f"{hashlib.sha1(question_pdf.encode('utf-8')).hexdigest()[:20]}.png"
            old_preview.unlink(missing_ok=True)
            
            dirs_to_rebuild.add(pdf_path.parent)
            
        # Rebuild each directory exactly once!
        for q_dir in dirs_to_rebuild:
            web_app.rebuild_test_pdf_from_questions(q_dir)

        return JSONResponse({"status": "ok"})


def easy_editor_html_page(data: dict) -> str:
    html = """<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kolay Soru Düzenleyici</title>
  FONT_LINKS_PLACEHOLDER
  <style>
    :root {
      --bg-dark: #0f172a;
      --bg-sidebar: #1e293b;
      --bg-canvas: #334155;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #10b981;
      --accent-hover: #059669;
      --border: #475569;
      --btn-bg: #475569;
      --btn-hover: #64748b;
      
      --box-q-border: #3b82f6;
      --box-q-bg: rgba(59, 130, 246, 0.15);
      --box-s-border: #a855f7;
      --box-s-bg: rgba(168, 85, 247, 0.15);
    }
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    body {
      font-family: 'Inter', sans-serif;
      background: var(--bg-dark);
      color: var(--text);
      display: flex;
      height: 100vh;
      overflow: hidden;
    }
    /* Sidebar */
    .sidebar {
      width: 340px;
      background: var(--bg-sidebar);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      height: 100vh;
      flex-shrink: 0;
    }
    .sidebar-header {
      padding: 20px;
      border-bottom: 1px solid var(--border);
    }
    .sidebar-header h1 {
      font-family: 'Outfit', sans-serif;
      font-size: 1.4rem;
      font-weight: 700;
      margin-bottom: 6px;
    }
    .sidebar-header p {
      font-size: 0.8rem;
      color: var(--text-muted);
      word-break: break-all;
    }
    .sidebar-section {
      padding: 20px;
      border-bottom: 1px solid var(--border);
    }
    .section-title {
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      margin-bottom: 12px;
      font-weight: 700;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .btn-add-test {
      background: none;
      border: none;
      color: var(--accent);
      font-weight: 600;
      cursor: pointer;
      font-size: 0.8rem;
    }
    .btn-add-test:hover {
      text-decoration: underline;
    }
    .test-select {
      width: 100%;
      padding: 10px;
      background: var(--bg-dark);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 8px;
      font-size: 0.9rem;
      margin-bottom: 10px;
    }
    .question-list {
      flex: 1;
      overflow-y: auto;
      padding: 20px;
    }
    .question-item-btn {
      width: 100%;
      padding: 10px 14px;
      background: var(--bg-dark);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 8px;
      margin-bottom: 8px;
      cursor: pointer;
      text-align: left;
      font-size: 0.85rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
      transition: background 0.2s, border-color 0.2s;
    }
    .question-item-btn:hover {
      background: #1e293b;
      border-color: var(--text-muted);
    }
    .question-item-btn.selected-item {
      border-color: #3b82f6;
      background: rgba(59, 130, 246, 0.1);
    }
    .question-item-btn.stem-item.selected-item {
      border-color: #a855f7;
      background: rgba(168, 85, 247, 0.1);
    }
    .question-item-btn span.badge {
      background: var(--border);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.75rem;
      color: var(--text-muted);
    }
    
    /* Workspace Area */
    .workspace {
      flex: 1;
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }
    .toolbar {
      min-height: 70px;
      height: auto;
      background: var(--bg-sidebar);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 30px;
      flex-shrink: 0;
      flex-wrap: wrap;
      gap: 10px 20px;
    }
    @media (max-width: 1200px) {
      .toolbar {
        padding: 10px 15px !important;
        gap: 10px;
        justify-content: center !important;
      }
      .tool-group {
        justify-content: center;
      }
      #tool-status-label {
        display: none !important;
      }
    }
    .tool-group {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .test-jump-label {
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--text-muted);
      font-size: 0.8rem;
      font-weight: 600;
      white-space: nowrap;
    }
    .test-jump-select {
      min-width: 150px;
      max-width: 220px;
      padding: 8px 30px 8px 10px;
      border: 1px solid var(--border);
      border-radius: 7px;
      background: var(--bg-dark);
      color: var(--text);
      font-size: 0.85rem;
      cursor: pointer;
    }
    .test-jump-select:focus {
      outline: 2px solid rgba(16, 185, 129, 0.45);
      border-color: var(--accent);
    }
    .btn {
      padding: 10px 20px;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      transition: background 0.2s;
    }
    .btn-primary {
      background: var(--accent);
      color: white;
    }
    .btn-primary:hover {
      background: var(--accent-hover);
    }
    .btn-secondary {
      background: var(--btn-bg);
      color: var(--text);
    }
    .btn-secondary:hover {
      background: var(--btn-hover);
    }
    .btn-danger {
      background: var(--danger);
      color: white;
    }
    .btn-danger:hover {
      background: #dc2626;
    }
    .tool-btn {
      background: none;
      border: 1px solid var(--border);
      color: var(--text);
      width: 44px;
      height: 44px;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.2rem;
      transition: background 0.2s, border-color 0.2s;
    }
    .tool-btn:hover {
      background: #334155;
    }
    .tool-btn.active {
      border-color: #3b82f6;
      background: rgba(59, 130, 246, 0.2);
      color: #3b82f6;
    }
    
    /* Pages Canvas Scroll Container */
    .canvas-container {
      flex: 1;
      overflow-y: auto;
      background: var(--bg-canvas);
      padding: 40px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 40px;
    }
    .page-wrapper {
      background: white;
      box-shadow: 0 10px 25px rgba(0,0,0,0.3);
      position: relative;
      user-select: none;
    }
    .page-header-badge {
      position: absolute;
      top: -30px;
      left: 0;
      background: var(--bg-sidebar);
      padding: 4px 12px;
      border-radius: 4px;
      font-size: 0.8rem;
      font-weight: 700;
      border: 1px solid var(--border);
    }
    .page-image {
      display: block;
      width: 100%;
      height: auto;
      pointer-events: none;
    }
    .drawing-overlay {
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      z-index: 5;
      cursor: default;
      touch-action: none;
    }
    .drawing-overlay.draw-active {
      cursor: crosshair;
    }
    
    /* Marker Rendering */
    .crop-marker {
      position: absolute;
      width: 18px;
      height: 18px;
      background: #ef4444;
      border: 2px solid white;
      border-radius: 50%;
      z-index: 10;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      color: white;
      font-size: 10px;
      font-weight: bold;
      transform: translate(-50%, -50%);
      box-shadow: 0 2px 4px rgba(0, 0, 0, 0.4);
      transition: transform 0.1s;
    }
    .crop-marker:hover {
      transform: translate(-50%, -50%) scale(1.2);
      background: #dc2626;
    }

    .two-point-temp-marker {
      position: absolute;
      width: 14px;
      height: 14px;
      background: #ff3b30;
      border: 2px solid white;
      border-radius: 50%;
      z-index: 1000;
      transform: translate(-50%, -50%);
      box-shadow: 0 0 4px rgba(0, 0, 0, 0.5);
      pointer-events: none;
      animation: pulse 1.5s infinite;
    }
    @keyframes pulse {
      0% { transform: translate(-50%, -50%) scale(1); opacity: 1; }
      50% { transform: translate(-50%, -50%) scale(1.3); opacity: 0.8; }
      100% { transform: translate(-50%, -50%) scale(1); opacity: 1; }
    }

    /* Box Rendering */
    .crop-box {
      position: absolute;
      border: 2px solid var(--box-q-border);
      background: var(--box-q-bg);
      z-index: 6;
      cursor: move;
      touch-action: none;
    }
    .crop-box.stem-box {
      border-color: var(--box-s-border);
      background: var(--box-s-bg);
    }
    .crop-box.selected {
      border-color: #f59e0b;
      box-shadow: 0 0 10px rgba(245, 158, 11, 0.5);
      z-index: 8;
    }
    .box-label {
      position: absolute;
      top: -22px;
      left: 0px;
      font-size: 0.75rem;
      background: rgba(15, 23, 42, 0.95);
      border: 1px solid var(--box-q-border);
      padding: 2px 6px;
      border-radius: 4px;
      font-weight: 700;
      color: white;
      pointer-events: none;
      white-space: nowrap;
      box-shadow: 0 2px 4px rgba(0,0,0,0.3);
    }
    .crop-box.stem-box .box-label {
      border-color: var(--box-s-border);
    }
    
    /* Custom Modal styling */
    .custom-modal {
      position: fixed;
      top: 0;
      left: 0;
      width: 100vw;
      height: 100vh;
      background: rgba(15, 23, 42, 0.6);
      backdrop-filter: blur(8px);
      -webkit-backdrop-filter: blur(8px);
      z-index: 1000;
      display: none;
      align-items: center;
      justify-content: center;
      opacity: 0;
      transition: opacity 0.2s ease;
    }
    .custom-modal.show {
      opacity: 1;
    }
    .custom-modal-content {
      background: var(--bg-sidebar);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      width: 400px;
      max-width: 90%;
      text-align: center;
      box-shadow: 0 20px 25px -5px rgb(0 0 0 / 0.5), 0 8px 10px -6px rgb(0 0 0 / 0.5);
      transform: scale(0.9);
      transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
    }
    .custom-modal.show .custom-modal-content {
      transform: scale(1);
    }
    .custom-modal-icon {
      font-size: 2.5rem;
      margin-bottom: 16px;
    }
    .custom-modal-text {
      font-size: 0.95rem;
      line-height: 1.5;
      color: var(--text);
      margin-bottom: 24px;
      font-weight: 500;
    }
    .custom-modal-buttons {
      display: flex;
      gap: 12px;
      justify-content: center;
    }
    .modal-btn {
      padding: 10px 20px;
      border-radius: 6px;
      font-size: 0.875rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: background 0.15s ease, transform 0.1s ease;
    }
    .modal-btn:active {
      transform: scale(0.97);
    }
    .modal-btn-cancel {
      background: var(--btn-bg);
      color: var(--text);
    }
    .modal-btn-cancel:hover {
      background: var(--btn-hover);
    }
    .modal-btn-ok {
      background: var(--accent);
      color: white;
    }
    .modal-btn-ok:hover {
      background: var(--accent-hover);
    }
    .box-delete {
      position: absolute;
      top: -12px;
      right: -12px;
      width: 20px;
      height: 20px;
      background: #ef4444;
      border: 1px solid white;
      border-radius: 50%;
      color: white;
      font-weight: bold;
      cursor: pointer;
      font-size: 0.85rem;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10;
      box-shadow: 0 2px 4px rgba(0,0,0,0.3);
      transition: transform 0.1s;
    }
    .box-delete:hover {
      transform: scale(1.15);
      background: #dc2626;
    }
    .box-action-btn {
      position: absolute;
      top: -12px;
      width: 20px;
      height: 20px;
      border: 1px solid white;
      border-radius: 50%;
      color: white;
      font-weight: bold;
      cursor: pointer;
      font-size: 0.85rem;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10;
      box-shadow: 0 2px 4px rgba(0,0,0,0.3);
      transition: transform 0.1s;
    }
    .box-action-btn:hover {
      transform: scale(1.15);
    }
    .box-action-btn.expand {
      right: 36px;
      background: #10b981;
    }
    .box-action-btn.expand:hover {
      background: #059669;
    }
    .box-action-btn.shrink {
      right: 12px;
      background: #3b82f6;
    }
    .box-action-btn.shrink:hover {
      background: #2563eb;
    }
    
    /* Resize Handles */
    .handle {
      position: absolute;
      width: 8px;
      height: 8px;
      background: white;
      border: 1.5px solid var(--box-q-border);
      border-radius: 2px;
      z-index: 9;
      touch-action: none;
    }
    .stem-box .handle {
      border-color: var(--box-s-border);
    }
    .handle-n  { top: -4px; left: calc(50% - 4px); cursor: ns-resize; }
    .handle-s  { bottom: -4px; left: calc(50% - 4px); cursor: ns-resize; }
    .handle-e  { right: -4px; top: calc(50% - 4px); cursor: ew-resize; }
    .handle-w  { left: -4px; top: calc(50% - 4px); cursor: ew-resize; }
    .handle-nw { top: -4px; left: -4px; cursor: nwse-resize; }
    .handle-ne { top: -4px; right: -4px; cursor: nesw-resize; }
    .handle-sw { bottom: -4px; left: -4px; cursor: nesw-resize; }
    .handle-se { bottom: -4px; right: -4px; cursor: nwse-resize; }
    
    /* Properties Dialog / Floating panel */
    .properties-panel {
      padding: 20px;
      border-top: 1px solid var(--border);
      background: #1e293b;
    }
    .properties-panel h3 {
      font-family: 'Outfit', sans-serif;
      font-size: 1.05rem;
      font-weight: 700;
      margin-bottom: 12px;
    }
    .prop-row {
      margin-bottom: 12px;
    }
    .prop-row label {
      display: block;
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-bottom: 4px;
      font-weight: 600;
    }
    .prop-row select,
    .prop-row input {
      width: 100%;
      padding: 8px;
      background: var(--bg-dark);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      font-size: 0.85rem;
    }
    .stem-checkbox-list {
      max-height: 120px;
      overflow-y: auto;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px;
      background: var(--bg-dark);
    }
    .stem-checkbox-list label {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 4px;
      font-size: 0.85rem;
      cursor: pointer;
      color: var(--text);
    }
    .stem-checkbox-list input {
      width: auto;
    }
    
    /* Temporary drawing rectangle */
    .temp-rect {
      position: absolute;
      border: 2px dashed #f59e0b;
      background: rgba(245, 158, 11, 0.1);
      pointer-events: none;
      z-index: 10;
    }
    
    /* Loading overlay */

    .btn-spinner {
      display: inline-block;
      width: 14px;
      height: 14px;
      border: 2px solid rgba(255,255,255,0.3);
      border-radius: 50%;
      border-top-color: white;
      animation: spin 0.8s infinite linear;
      margin-right: 8px;
      vertical-align: middle;
    }
    @keyframes spin {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }
    
    /* Sidebar Collapsed State */
    .sidebar {
      transition: width 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .sidebar.collapsed {
      width: 0;
      border-right: none;
      overflow: hidden;
    }
    
    /* Accordion Layout */
    .accordion-item {
      display: flex;
      flex-direction: column;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
      transition: flex 0.3s ease;
    }
    .accordion-item.active {
      flex: 1 1 auto;
      overflow: hidden;
    }
    .accordion-header {
      padding: 14px 20px;
      background: #1e293b;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      user-select: none;
      font-size: 0.8rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      border-bottom: 1px solid transparent;
      transition: background 0.2s, color 0.2s;
    }
    .accordion-header:hover {
      background: #334155;
      color: var(--text);
    }
    .accordion-item.active .accordion-header {
      border-bottom: 1px solid var(--border);
      color: var(--text);
      background: #0f172a;
    }
    .accordion-arrow {
      font-size: 0.7rem;
      transition: transform 0.3s ease;
      color: var(--text-muted);
    }
    .accordion-item.active .accordion-arrow {
      transform: rotate(180deg);
      color: var(--text);
    }
    .accordion-content {
      display: none;
      flex-direction: column;
      overflow: hidden;
      background: var(--bg-sidebar);
    }
    .accordion-item.active .accordion-content {
      display: flex;
      flex: 1 1 auto;
      overflow-y: auto;
    }
    
    /* Canli Log Konsolu Ozellestirmeleri */
    #log-console-container {
      flex: 1 1 auto;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    #log-console {
      scrollbar-width: none; /* Firefox */
      -ms-overflow-style: none; /* IE/Edge */
      word-break: break-all;
      overflow-wrap: anywhere;
    }
    #log-console::-webkit-scrollbar {
      display: none; /* Chrome/Safari */
    }
  </style>
</head>
<body>



  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-header" style="flex-shrink: 0;">
      <h1>Kolay Soru Editörü</h1>
      <p id="pdf-title-label"></p>
    </div>
    
    <!-- Accordion Wrapper -->
    <div style="display: flex; flex-direction: column; flex: 1 1 auto; overflow: hidden;">
      
      <!-- Accordion Item: Tests -->
      <div class="accordion-item active" id="acc-tests">
        <div class="accordion-header" onclick="toggleAccordion('acc-tests')">
          <span>Testler</span>
          <span class="accordion-arrow">▼</span>
        </div>
        <div class="accordion-content" style="padding: 15px 20px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
            <span style="font-size: 0.8rem; color: var(--text-muted); font-weight: bold; text-transform: uppercase;">Test Seçimi</span>
            <button class="btn-add-test" onclick="addNewTestAutomatically()">+ Yeni Ekle</button>
          </div>
          <select class="test-select" id="test-selector" onchange="handleTestChange(this.value)">
          </select>
          <div style="display: flex; gap: 8px; margin-top: 8px;">
            <button class="btn btn-secondary" style="flex: 1; padding: 6px 12px; font-size: 0.8rem; justify-content: center; align-items: center;" onclick="switchToPrevTest()">◀ Önceki</button>
            <button class="btn btn-secondary" style="flex: 1; padding: 6px 12px; font-size: 0.8rem; justify-content: center; align-items: center;" onclick="switchToNextTest()">Sonraki ▶</button>
            <button class="btn btn-danger" style="flex: 1; padding: 6px 12px; font-size: 0.8rem; justify-content: center; align-items: center; background:#ef4444;" onclick="deleteActiveTest()">Sil 🗑️</button>
          </div>
        </div>
      </div>
      
      <!-- Accordion Item: Questions -->
      <div class="accordion-item" id="acc-questions">
        <div class="accordion-header" onclick="toggleAccordion('acc-questions')">
          <span>Sorular</span>
          <span class="accordion-arrow">▼</span>
        </div>
        <div class="accordion-content">
          <div class="question-list" id="question-list-container" style="padding: 15px 20px; flex: 1 1 auto; overflow-y: auto;">
          </div>
        </div>
      </div>
      
      <!-- Accordion Item: Properties -->
      <div class="accordion-item" id="acc-properties" style="display: none;">
        <div class="accordion-header" onclick="toggleAccordion('acc-properties')">
          <span id="acc-prop-title-text">Özellikler</span>
          <span class="accordion-arrow">▼</span>
        </div>
        <div class="accordion-content" style="padding: 15px 20px;">
          <div class="properties-panel" id="properties-panel" style="border: none; padding: 0; background: transparent;">
            <h3 id="prop-title" style="display: none;">Özellikler</h3>
            
            <!-- Change Test -->
            <div class="prop-row">
              <label>Ait Olduğu Test</label>
              <select id="prop-test-select" onchange="changeSelectedBoxTest(this.value)">
              </select>
            </div>
            
            <!-- Change Question Number (Soru No) -->
            <div class="prop-row" id="prop-soru-no-row" style="display:none;">
              <label>Soru Numarası</label>
              <input type="number" id="prop-soru-no-input" min="1" max="999" onchange="changeSelectedBoxSoruNo(this.value)">
            </div>
            
            <!-- Link Common Stem for Question -->
            <div class="prop-row" id="prop-stem-link-row" style="display:none;">
              <label>Ortak Kök İlişkilendir</label>
              <select id="prop-stem-select" onchange="linkQuestionToStem(this.value)">
              </select>
            </div>
            
            <!-- Link Target Questions for Common Stem -->
            <div class="prop-row" id="prop-stem-targets-row" style="display:none;">
              <label>Ortak Kökü Uygula (Sorular)</label>
              <div class="stem-checkbox-list" id="prop-stem-checkboxes">
              </div>
            </div>
            
            <!-- Common Stem Placement Direction -->
            <div class="prop-row" id="prop-stem-placement-row" style="display:none;">
              <label>Birleştirme Yönü</label>
              <select id="prop-stem-placement" onchange="changeSelectedStemPlacement(this.value)">
                <option value="top">Alt Alta (Dikey)</option>
                <option value="left">Yan Yana (Yatay)</option>
              </select>
            </div>
            
            <!-- Sub-crops Section for Question -->
            <div id="prop-subcrops-section" style="display:none; border-top:1px dashed var(--border); margin-top:12px; padding-top:12px;">
              <label style="display:block; font-size:0.8rem; color:var(--text-muted); margin-bottom:6px; font-weight:600;">Alt Kırpık Parçalar (Sub-crops)</label>
              <button class="btn btn-secondary" style="width:100%; margin-bottom:10px; border-color:var(--accent); color:var(--accent); justify-content:center; padding: 6px 12px; font-size: 0.8rem;" onclick="startDrawSubcrop()">✂️ Yeni Parça Ekle (Çiz)</button>
              <div id="prop-subcrops-list" style="display:flex; flex-direction:column; gap:8px; margin-bottom:12px;"></div>
            </div>

            <!-- Adjust Bounds (Expand/Optimize) -->
            <div class="prop-row" style="display:flex; gap:10px; margin-top:12px; border-top:1px dashed var(--border); padding-top:12px;">
              <button class="btn btn-secondary" style="flex:1; justify-content:center; padding: 6px 12px; font-size: 0.8rem;" onclick="adjustSelectedBoxSmart('expand')">➕ Genişlet (Akıllı)</button>
              <button class="btn btn-secondary" style="flex:1; justify-content:center; padding: 6px 12px; font-size: 0.8rem;" onclick="adjustSelectedBoxSmart('shrink')">➖ Daralt (Metne Hizala)</button>
            </div>

            <!-- Delete Button -->
            <button class="btn btn-danger" style="width:100%; margin-top:10px;" onclick="deleteSelectedBox()">Kutuyu Sil</button>
          </div>
        </div>
      </div>
      
      <!-- Accordion Item: Logs -->
      <div class="accordion-item" id="acc-logs">
        <div class="accordion-header" onclick="toggleAccordion('acc-logs')" style="position: relative; display: flex; justify-content: space-between; align-items: center; width: 100%;">
          <span>Canlı Kesim Logları</span>
          <div style="display: flex; align-items: center; gap: 10px;">
            <button onclick="downloadLogs(event)" title="Logları İndir" style="background: none; border: none; cursor: pointer; font-size: 1rem; color: var(--text-muted); padding: 2px; display: inline-flex; align-items: center; justify-content: center; transition: color 0.2s;" onmouseover="this.style.color='var(--text)'" onmouseout="this.style.color='var(--text-muted)'">📥</button>
            <span class="accordion-arrow">▼</span>
          </div>
        </div>
        <div class="accordion-content">
          <div id="log-console-container" style="background:#0b0f19; flex:1; display:flex; flex-direction:column; overflow:hidden;">
            <pre id="log-console" style="flex:1; overflow-y:auto; padding:10px; font-family:monospace; font-size:0.75rem; color:#10b981; margin:0; white-space:pre-wrap; text-align:left;"></pre>
          </div>
        </div>
      </div>
      
    </div>
  </aside>

  <!-- Workspace -->
  <main class="workspace">
    <!-- Live Progress Banner -->
    <div id="status-banner" style="background:#1e293b; padding:12px 20px; border-bottom:1px solid var(--border); display:flex; justify-content:space-between; align-items:center; flex-shrink:0;">
      <div style="display:flex; align-items:center; gap:10px;">
        <span id="status-icon">⏳</span>
        <strong id="status-title" style="font-family:'Outfit';">PDF Kesiliyor...</strong>
        <span id="status-text" style="color:var(--text-muted); font-size:0.9rem;">Analiz başlatılıyor</span>
      </div>
      <div style="display:flex; align-items:center; gap:15px;">
        <!-- Progress bar container -->
        <div id="progress-container" style="width:150px; height:8px; background:var(--border); border-radius:4px; overflow:hidden;">
          <div id="progress-bar" style="width:0%; height:100%; background:var(--accent); transition:width 0.3s;"></div>
        </div>
        <span id="progress-text" style="font-size:0.85rem; font-weight:bold; color:var(--text-muted);">0%</span>
      </div>
    </div>

    <header class="toolbar">
      <!-- Left side tools -->
      <div class="tool-group">
        <button class="tool-btn" id="btn-toggle-sidebar" onclick="toggleSidebar()" title="Sol Menüyü Gizle (Kısayol: B)" style="margin-right: 10px;">
          <span>◀</span>
        </button>
        <button class="tool-btn active" id="tool-select" onclick="setTool('select')" title="Kutu Seç & Düzenle (Kısayol: S)">
          <span>🖱️</span>
        </button>
        <button class="tool-btn" id="tool-draw-q" onclick="setTool('draw-question')" title="Yeni Soru Çiz (Kısayol: Q)">
          <span>✂️</span>
        </button>
        <button class="tool-btn" id="tool-draw-s" onclick="setTool('draw-stem')" title="Yeni Ortak Kök Çiz (Kısayol: W)">
          <span>🖼️</span>
        </button>
        <button class="tool-btn" id="tool-add-marker" onclick="setTool('add-marker')" title="Soru Başlangıç İşareti Ekle (Kısayol: E)">
          <span>📍</span>
        </button>
        <button class="tool-btn" id="tool-two-points" onclick="setTool('two-points')" title="İki Nokta ile Soru Çiz (Kısayol: X)">
          <span>🎯</span>
        </button>
        <span style="color:var(--text-muted); font-size:0.85rem; margin-left: 10px;" id="tool-status-label">Mod: Seç & Sürükle</span>
      </div>

      <!-- Undo / Redo tools -->
      <div class="tool-group">
        <button class="tool-btn" id="btn-undo" onclick="undo()" title="Geri Al (Ctrl+Z)" disabled style="opacity: 0.4; cursor: not-allowed;">
          <span>↩️</span>
        </button>
        <button class="tool-btn" id="btn-redo" onclick="redo()" title="İleri Al (Ctrl+Y)" disabled style="opacity: 0.4; cursor: not-allowed;">
          <span>↪️</span>
        </button>
      </div>

      <!-- Test navigation independent from question/test assignment -->
      <div class="tool-group" id="test-jump-group">
        <label class="test-jump-label" for="test-jump-select">
          <span>📚 Denemeye Git</span>
          <select id="test-jump-select" class="test-jump-select" onchange="jumpToNavigationTest(this.value)">
            <option value="">Denemeler algılanıyor...</option>
          </select>
        </label>
      </div>
      
      <!-- Toggle for Auto Numbering -->
      <div class="tool-group">
        <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; cursor:pointer; user-select:none;" title="Soruları otomatik olarak soldan sağa ve yukarıdan aşağıya numaralandırır. Kapatırsanız soru numaralarını manuel ayarlayabilirsiniz.">
          <input type="checkbox" id="toggle-auto-numbering" checked style="width:16px; height:16px; accent-color:var(--accent);" onchange="reindexQuestions()">
          <span>Otomatik Numaralandır</span>
        </label>
      </div>
      
      <!-- Toggle for Hide Question Numbers -->
      <div class="tool-group">
        <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; cursor:pointer; user-select:none;">
          <input type="checkbox" id="toggle-hide-numbers" style="width:16px; height:16px; accent-color:var(--accent);">
          <span>Soru Numaralarını Gizle</span>
        </label>
      </div>
      
      <!-- Save & Return Actions -->
      <div class="tool-group">
        <button class="btn btn-secondary" onclick="showHelpModal()" style="margin-right: 8px;">❓ Nasıl Kullanılır?</button>
        <button class="btn btn-secondary" onclick="window.location.href='/'">Vazgeç / Yeni İş</button>
        <button class="btn btn-primary" id="btn-save-edits" onclick="saveEdits()" disabled style="opacity: 0.6; cursor: not-allowed;">Tamamla ve İndir</button>
      </div>
    </header>
    
    <!-- Canvas container scrollable -->
    <div class="canvas-container" id="canvas-container">
    </div>
  </main>

  <script>
    function toggleAccordion(itemId) {
      const items = document.querySelectorAll('.accordion-item');
      items.forEach(item => {
        if (item.id === itemId) {
          item.classList.toggle('active');
        } else {
          item.classList.remove('active');
        }
      });
    }

    function openAccordion(itemId) {
      const items = document.querySelectorAll('.accordion-item');
      items.forEach(item => {
        if (item.id === itemId) {
          item.classList.add('active');
        } else {
          item.classList.remove('active');
        }
      });
    }

    function toggleSidebar() {
      const sidebar = document.querySelector('.sidebar');
      if (!sidebar) return;
      sidebar.classList.toggle('collapsed');
      
      const btn = document.getElementById('btn-toggle-sidebar');
      if (!btn) return;
      
      if (sidebar.classList.contains('collapsed')) {
        btn.innerHTML = '<span>▶</span>';
        btn.title = "Sol Menüyü Göster (Kısayol: B)";
      } else {
        btn.innerHTML = '<span>◀</span>';
        btn.title = "Sol Menüyü Gizle (Kısayol: B)";
      }
    }

    async function saveWithDesktopBridge(url, filename) {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api.save_download !== 'function') return false;
      const result = await api.save_download(url, filename);
      if (!result || !result.ok) {
        throw new Error((result && result.error) || 'Dosya kaydedilemedi.');
      }
      return true;
    }

    async function downloadLogs(event) {
      if (event) event.stopPropagation();
      const filename = `job_${jobId}_logs.txt`;
      try {
        if (await saveWithDesktopBridge(`/jobs/${jobId}/download/log`, filename)) return;
      } catch (err) {
        await showAlert("Log kaydı indirilemedi: " + err.message);
        return;
      }
      const logConsole = document.getElementById('log-console');
      if (!logConsole) return;
      const logText = logConsole.textContent || logConsole.innerText || "";
      
      const blob = new Blob([logText], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    }

    const serverData = JSON_DATA_PLACEHOLDER;
    const jobId = serverData.job_id;
    const pdfName = serverData.pdf_name;
    const pages = serverData.pages;
    const navigationTests = Array.isArray(serverData.navigation_tests) ? serverData.navigation_tests : [];
    const pagesMap = {};
    pages.forEach(p => {
      pagesMap[p.page_idx] = p;
    });
    
    let isFirstPageLoaded = false;
    let lastJobStatus = serverData.status;
    

    
    function selectTest(testNo) {
      activeTestNo = testNo;
      const selectEl = document.querySelector('.test-select');
      if (selectEl) {
        selectEl.value = testNo;
      }
      renderCanvasBoxes();
      renderQuestionsList();
      updatePageTestButtons();
      scrollToFirstPageOfTest(testNo);
    }
    
    function updatePageTestButtons() {
      pages.forEach(p => {
        const badge = document.querySelector(`#page_wrap_${p.page_idx} .page-header-badge`);
        if (!badge) return;
        
        badge.querySelectorAll('.page-test-btn').forEach(el => el.remove());
        
        const testsStartingOnThisPage = [];
        tests.forEach(t => {
          const pageIdxs = questions.filter(q => q.test_no === t.test_no).map(q => q.page_idx);
          if (pageIdxs.length > 0) {
            const minPageIdx = Math.min(...pageIdxs);
            if (minPageIdx === p.page_idx) {
              testsStartingOnThisPage.push(t);
            }
          }
        });
        
        testsStartingOnThisPage.forEach(t => {
          const testBtn = document.createElement('button');
          testBtn.className = 'page-test-btn';
          testBtn.textContent = `📂 ${t.test_name}`;
          
          if (t.test_no === activeTestNo) {
            testBtn.style.background = 'var(--accent)';
          } else {
            testBtn.style.background = '#475569';
          }
          testBtn.style.color = 'white';
          testBtn.style.border = 'none';
          testBtn.style.padding = '4px 8px';
          testBtn.style.borderRadius = '4px';
          testBtn.style.cursor = 'pointer';
          testBtn.style.fontSize = '0.8rem';
          testBtn.style.fontWeight = 'bold';
          testBtn.style.marginLeft = '8px';
          testBtn.onclick = (e) => {
            e.stopPropagation();
            selectTest(t.test_no);
          };
          badge.appendChild(testBtn);
        });
      });
    }
    
    let activeTestNo = serverData.tests.length > 0 ? serverData.tests[0].test_no : 1;
    let activeTool = 'select'; // 'select', 'draw-question', 'draw-stem'
    let pageMarkers = {};
    let twoPointClicks = {};
    
    let questions = serverData.questions.map((q, idx) => {
      const sub_crops = (q.sub_crops || []).map((sub, sidx) => ({
        ...sub,
        tempId: 'sub_' + idx + '_' + sidx + '_' + Math.floor(Math.random() * 100000)
      }));
      return { ...q, tempId: 'q_' + idx, sub_crops: sub_crops };
    });
    let tests = [...serverData.tests];
    let questionsLoaded = (questions.length > 0);
    const modifiedPages = new Set();
    
    // Group stems from loaded questions:
    let stems = [];
    questions.forEach(q => {
      if (q.common_stem) {
        let existing = stems.find(s => 
          s.test_no === q.test_no &&
          s.page_idx === q.common_stem.page_idx &&
          Math.abs(s.crop_left - q.common_stem.crop_left) < 1 &&
          Math.abs(s.crop_top - q.common_stem.crop_top) < 1 &&
          Math.abs(s.crop_right - q.common_stem.crop_right) < 1 &&
          Math.abs(s.crop_bottom - q.common_stem.crop_bottom) < 1
        );
        if (existing) {
          if (!existing.targetTempIds.includes(q.tempId)) {
            existing.targetTempIds.push(q.tempId);
          }
        } else {
          stems.push({
            tempId: 's_' + stems.length,
            test_no: q.test_no,
            page_idx: q.common_stem.page_idx,
            crop_left: q.common_stem.crop_left,
            crop_top: q.common_stem.crop_top,
            crop_right: q.common_stem.crop_right,
            crop_bottom: q.common_stem.crop_bottom,
            placement: q.common_stem.placement || 'top',
            targetTempIds: [q.tempId]
          });
        }
      }
    });
    
    let selectedBoxId = null; // can be q_... or s_...
    
    // Undo / Redo Stacks
    let undoStack = [];
    let redoStack = [];
    
    function saveState() {
      const snapshot = {
        questions: JSON.parse(JSON.stringify(questions)),
        stems: JSON.parse(JSON.stringify(stems)),
        modifiedPages: Array.from(modifiedPages),
        activeTestNo: activeTestNo,
        selectedBoxId: selectedBoxId
      };
      const snapStr = JSON.stringify(snapshot);
      
      if (undoStack.length > 0) {
        const lastSnap = JSON.parse(undoStack[undoStack.length - 1]);
        const questionsChanged = JSON.stringify(lastSnap.questions) !== JSON.stringify(snapshot.questions);
        const stemsChanged = JSON.stringify(lastSnap.stems) !== JSON.stringify(snapshot.stems);
        
        if (!questionsChanged && !stemsChanged) {
          // Only view states or active selection changed, update last snapshot in place
          undoStack[undoStack.length - 1] = snapStr;
          return;
        }
      }
      
      undoStack.push(snapStr);
      redoStack = [];
      updateUndoRedoButtons();
    }
    
    function restoreState(snapStr) {
      const snapshot = JSON.parse(snapStr);
      questions = snapshot.questions;
      stems = snapshot.stems;
      
      modifiedPages.clear();
      if (snapshot.modifiedPages) {
        snapshot.modifiedPages.forEach(p => modifiedPages.add(p));
      }
      
      activeTestNo = snapshot.activeTestNo;
      selectedBoxId = snapshot.selectedBoxId;
      
      // Update UI
      reindexQuestionsWithoutSave();
      renderTestSelectors();
      if (selectedBoxId) {
        showPropertiesPanel(selectedBoxId);
      } else {
        hidePropertiesPanel();
      }
      updateUndoRedoButtons();
    }
    
    function undo() {
      if (undoStack.length <= 1) return;
      const current = undoStack.pop();
      redoStack.push(current);
      const prev = undoStack[undoStack.length - 1];
      restoreState(prev);
    }
    
    function redo() {
      if (redoStack.length === 0) return;
      const next = redoStack.pop();
      undoStack.push(next);
      restoreState(next);
    }
    
    function updateUndoRedoButtons() {
      const undoBtn = document.getElementById('btn-undo');
      const redoBtn = document.getElementById('btn-redo');
      if (!undoBtn || !redoBtn) return;
      
      if (undoStack.length > 1) {
        undoBtn.disabled = false;
        undoBtn.style.opacity = '1';
        undoBtn.style.cursor = 'pointer';
      } else {
        undoBtn.disabled = true;
        undoBtn.style.opacity = '0.4';
        undoBtn.style.cursor = 'not-allowed';
      }
      
      if (redoStack.length > 0) {
        redoBtn.disabled = false;
        redoBtn.style.opacity = '1';
        redoBtn.style.cursor = 'pointer';
      } else {
        redoBtn.disabled = true;
        redoBtn.style.opacity = '0.4';
        redoBtn.style.cursor = 'not-allowed';
      }
    }
    
    // Drag & Resize tracking
    let isDragging = false;
    let isResizing = false;
    let dragStartMouse = { x: 0, y: 0 };
    let dragStartBox = { left: 0, top: 0, width: 0, height: 0 };
    let resizeHandle = null;
    let trackingBoxId = null;
    
    // Drawing tracking
    let isDrawing = false;
    let drawStart = { x: 0, y: 0 };
    let drawActivePageIdx = null;
    
    // Initialize UI
    document.getElementById('pdf-title-label').textContent = pdfName;
    document.getElementById('toggle-hide-numbers').checked = serverData.hide_question_numbers;
    document.getElementById('toggle-hide-numbers').addEventListener('change', () => {
      renderCanvasBoxes();
    });
    renderPages();
    renderTestNavigation();
    renderTestSelectors();
    // Detect custom numbering
    // Automatic analysis already extracted the printed question numbers.
    // Preserve them even when a question is missed; spatial re-numbering would
    // silently shift every following label to the wrong source question.
    let hasCustomNumbering = serverData.workflow_mode !== 'manual';
    const testGroups = {};
    questions.forEach(q => {
      if (!testGroups[q.test_no]) testGroups[q.test_no] = [];
      testGroups[q.test_no].push(q);
    });
    for (const tNo in testGroups) {
      const group = testGroups[tNo];
      group.sort((a, b) => {
        if (a.page_idx !== b.page_idx) return a.page_idx - b.page_idx;
        const page = pagesMap[a.page_idx];
        const mid = page ? (page.width / 2) : 0;
        const getCol = (q) => {
          const isLeft = q.crop_right <= mid + 40;
          const isRight = q.crop_left >= mid - 40;
          if (isLeft && !isRight) return 0;
          if (isRight && !isLeft) return 1;
          return -1;
        };
        const colA = getCol(a);
        const colB = getCol(b);
        if (colA !== colB) {
          if (colA === -1 || colB === -1) return a.crop_top - b.crop_top;
          return colA - colB;
        }
        return a.crop_top - b.crop_top;
      });
      group.forEach((q, idx) => {
        if (parseInt(q.soru_no) !== idx + 1) {
          hasCustomNumbering = true;
        }
      });
    }
    document.getElementById('toggle-auto-numbering').checked = !hasCustomNumbering;
    reindexQuestions(); // sorts and numbers questions initially
    
    // Start Polling Status
    let isProcessing = ['queued', 'rendering', 'processing'].includes(serverData.status);
    let pollInterval = isProcessing ? setInterval(checkJobStatus, 1500) : null;
    checkJobStatus();
    
    // Rendering logic
    function renderTestSelectors() {
      const selector = document.getElementById('test-selector');
      if (selector) {
        selector.innerHTML = '';
        tests.forEach(t => {
          const opt = document.createElement('option');
          opt.value = t.test_no;
          opt.textContent = t.test_name;
          opt.selected = t.test_no === activeTestNo;
          selector.appendChild(opt);
        });
        selector.value = activeTestNo; // Explicit sync!
      }
      
      // Update property panel selector too
      const propSel = document.getElementById('prop-test-select');
      if (propSel) {
        propSel.innerHTML = '';
        tests.forEach(t => {
          const opt = document.createElement('option');
          opt.value = t.test_no;
          opt.textContent = t.test_name;
          propSel.appendChild(opt);
        });
        if (selectedBoxId) {
          const q = questions.find(item => item.tempId === selectedBoxId) || stems.find(item => item.tempId === selectedBoxId);
          if (q) {
            propSel.value = q.test_no;
          }
        }
      }
    }

    function scrollToFirstPageOfTest(testNo) {
      const testQs = questions.filter(q => q.test_no === testNo);
      const testStems = stems.filter(s => s.test_no === testNo);
      const pageIdxs = [
        ...testQs.map(q => q.page_idx),
        ...testStems.map(s => s.page_idx)
      ];
      if (pageIdxs.length > 0) {
        const minPageIdx = Math.min(...pageIdxs);
        const pageWrap = document.getElementById('page_wrap_' + minPageIdx);
        if (pageWrap) {
          pageWrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      }
    }

    function renderTestNavigation() {
      const selector = document.getElementById('test-jump-select');
      if (!selector) return;
      selector.innerHTML = '';

      const placeholder = document.createElement('option');
      placeholder.value = '';
      placeholder.textContent = navigationTests.length > 0
        ? `Deneme seç (${navigationTests.length})`
        : 'Deneme bulunamadı';
      placeholder.selected = true;
      selector.appendChild(placeholder);

      navigationTests.forEach(test => {
        const option = document.createElement('option');
        option.value = String(test.test_no);
        option.textContent = `${test.label} · Sayfa ${Number(test.start_page_idx) + 1}`;
        selector.appendChild(option);
      });
      selector.disabled = navigationTests.length === 0;
    }

    function jumpToNavigationTest(testNoValue) {
      if (!testNoValue) return;
      const testNo = Number(testNoValue);
      const target = navigationTests.find(test => Number(test.test_no) === testNo);
      if (!target) return;

      const startIdx = Number(target.start_page_idx);
      const endIdx = Number(target.end_page_idx);
      const visiblePage = pages.find(page => page.page_idx >= startIdx && page.page_idx <= endIdx);
      if (!visiblePage) {
        showAlert('Bu denemenin sayfaları başlangıçta seçilen sayfalar arasında değil.');
        return;
      }

      const pageWrap = document.getElementById('page_wrap_' + visiblePage.page_idx);
      if (pageWrap) {
        pageWrap.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }
    
    function addNewTestAutomatically() {
      const nextNo = tests.length > 0 ? Math.max(...tests.map(t => t.test_no)) + 1 : 1;
      const name = `Test-${nextNo}`;
      tests.push({ test_no: nextNo, test_name: name });
      activeTestNo = nextNo;
      renderTestSelectors();
      reindexQuestions();
      updatePageTestButtons();
    }

    async function deleteActiveTest() {
      if (tests.length <= 1) {
        await showAlert("En az bir test kalmalıdır. Son testi silemezsiniz.");
        return;
      }
      
      const activeTestObj = tests.find(t => t.test_no === activeTestNo);
      if (!activeTestObj) return;
      
      const isConfirmed = await showConfirm(`"${activeTestObj.test_name}" testini ve bu teste ait tüm soruları/kökleri silmek istediğinize emin misiniz?`);
      if (isConfirmed) {
        questions.forEach(q => {
          if (q.test_no === activeTestNo) {
            modifiedPages.add(q.page_idx);
          }
        });
        stems.forEach(s => {
          if (s.test_no === activeTestNo) {
            modifiedPages.add(s.page_idx);
          }
        });
        
        questions = questions.filter(q => q.test_no !== activeTestNo);
        stems = stems.filter(s => s.test_no !== activeTestNo);
        
        const remainingTests = tests.filter(t => t.test_no !== activeTestNo);
        remainingTests.sort((a, b) => a.test_no - b.test_no);
        
        const testMapping = {};
        const newTests = [];
        remainingTests.forEach((t, index) => {
          const newNo = index + 1;
          testMapping[t.test_no] = {
            newNo: newNo,
            newName: `Test-${newNo}`
          };
          newTests.push({
            test_no: newNo,
            test_name: `Test-${newNo}`
          });
        });
        
        questions.forEach(q => {
          const mapping = testMapping[q.test_no];
          if (mapping) {
            modifiedPages.add(q.page_idx);
            q.test_no = mapping.newNo;
            q.test_name = mapping.newName;
          }
        });
        stems.forEach(s => {
          const mapping = testMapping[s.test_no];
          if (mapping) {
            modifiedPages.add(s.page_idx);
            s.test_no = mapping.newNo;
          }
        });
        
        tests = newTests;
        activeTestNo = tests[0].test_no;
        
        selectedBoxId = null;
        hidePropertiesPanel();
        renderTestSelectors();
        reindexQuestions();
        updatePageTestButtons();
      }
    }
    
    function handleTestChange(val) {
      activeTestNo = parseInt(val);
      selectedBoxId = null;
      hidePropertiesPanel();
      reindexQuestions();
      updatePageTestButtons();
      scrollToFirstPageOfTest(activeTestNo);
    }
    
    function switchToPrevTest() {
      const selector = document.getElementById('test-selector');
      const currentIndex = selector.selectedIndex;
      if (currentIndex > 0) {
        selector.selectedIndex = currentIndex - 1;
        handleTestChange(selector.value);
      }
    }
    
    function switchToNextTest() {
      const selector = document.getElementById('test-selector');
      const currentIndex = selector.selectedIndex;
      if (currentIndex < selector.options.length - 1) {
        selector.selectedIndex = currentIndex + 1;
        handleTestChange(selector.value);
      }
    }

    // Keyboard shortcuts for tools: Select (S), Question (Q), Common Stem (W)
    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') {
        return;
      }
      
      // Handle Undo / Redo keyboard shortcuts
      if ((e.ctrlKey || e.metaKey) && !e.altKey) {
        const key = e.key.toLowerCase();
        if (key === 'z') {
          e.preventDefault();
          if (e.shiftKey) {
            redo();
          } else {
            undo();
          }
        } else if (key === 'y') {
          e.preventDefault();
          redo();
        }
        return;
      }
      
      if (e.ctrlKey || e.metaKey || e.altKey) {
        return;
      }
      
      const key = e.key.toLowerCase();
      if (key === 's') {
        setTool('select');
      } else if (key === 'q') {
        setTool('draw-question');
      } else if (key === 'w') {
        setTool('draw-stem');
      } else if (key === 'e') {
        setTool('add-marker');
      } else if (key === 'x') {
        setTool('two-points');
      } else if (key === 'b') {
        toggleSidebar();
      }
    });
    
    function setTool(tool) {
      activeTool = tool;
      document.querySelectorAll('.tool-btn').forEach(btn => btn.classList.remove('active'));
      let label = "Mod: Seç & Sürükle";
      if (tool === 'draw-question') {
        document.getElementById('tool-draw-q').classList.add('active');
        label = "Mod: Soru Çiz (+)";
      } else if (tool === 'draw-stem') {
        document.getElementById('tool-draw-s').classList.add('active');
        label = "Mod: Ortak Kök Çiz (+)";
      } else if (tool === 'add-marker') {
        document.getElementById('tool-add-marker').classList.add('active');
        label = "Mod: İşaret Noktası Ekle (📍)";
      } else if (tool === 'two-points') {
        document.getElementById('tool-two-points').classList.add('active');
        label = "Mod: İki Nokta ile Soru Çiz (🎯)";
      } else if (tool === 'draw-subcrop') {
        label = "Mod: Alt Parça Çiz (Yeşil Kesikli)";
      } else {
        document.getElementById('tool-select').classList.add('active');
      }
      document.getElementById('tool-status-label').textContent = label;

      // Clear two point clicks and markers when changing tools
      document.querySelectorAll('.two-point-temp-marker').forEach(el => el.remove());
      twoPointClicks = {};
      
      // Update visual indicator
      document.querySelectorAll('.drawing-overlay').forEach(el => {
        if (tool !== 'select') {
          el.classList.add('draw-active');
        } else {
          el.classList.remove('draw-active');
        }
      });
    }
    
    function updateAllQuestionsCommonStemFromStems() {
      // Clear common_stem for all questions
      questions.forEach(q => {
        q.common_stem = null;
      });
      // Assign common_stem based on current stems list
      stems.forEach(s => {
        s.targetTempIds.forEach(tid => {
          const q = questions.find(item => item.tempId === tid);
          if (q) {
            q.common_stem = {
              page_idx: s.page_idx,
              crop_left: s.crop_left,
              crop_top: s.crop_top,
              crop_right: s.crop_right,
              crop_bottom: s.crop_bottom,
              placement: s.placement || 'top'
            };
          }
        });
      });
    }

    // Sort and re-number questions physically page by page
    function reindexQuestions() {
      reindexQuestionsWithoutSave();
      saveState();
    }

    function sortAndRenameTests() {
      const testMinPages = {};
      tests.forEach(t => {
        const tNo = parseInt(t.test_no);
        testMinPages[tNo] = Infinity;
      });
      questions.forEach(q => {
        const qNo = parseInt(q.test_no);
        if (testMinPages[qNo] !== undefined) {
          testMinPages[qNo] = Math.min(testMinPages[qNo], q.page_idx);
        }
      });
      stems.forEach(s => {
        const sNo = parseInt(s.test_no);
        if (testMinPages[sNo] !== undefined) {
          testMinPages[sNo] = Math.min(testMinPages[sNo], s.page_idx);
        }
      });
      tests.forEach(t => {
        const tNo = parseInt(t.test_no);
        if (testMinPages[tNo] === Infinity) {
          testMinPages[tNo] = tNo * 1000;
        }
      });
      
      const sortedTests = [...tests].sort((a, b) => {
        return testMinPages[parseInt(a.test_no)] - testMinPages[parseInt(b.test_no)];
      });
      
      const oldToNewMap = {};
      const updatedTests = sortedTests.map((t, idx) => {
        const newNo = idx + 1;
        const newName = 'Test-' + newNo;
        oldToNewMap[parseInt(t.test_no)] = { test_no: newNo, test_name: newName };
        return {
          test_no: newNo,
          test_name: newName
        };
      });
      
      questions.forEach(q => {
        const mapping = oldToNewMap[parseInt(q.test_no)];
        if (mapping) {
          q.test_no = mapping.test_no;
          q.test_name = mapping.test_name;
        }
      });
      stems.forEach(s => {
        const mapping = oldToNewMap[parseInt(s.test_no)];
        if (mapping) {
          s.test_no = mapping.test_no;
          s.test_name = mapping.test_name;
        }
      });
      
      const activeMapping = oldToNewMap[parseInt(activeTestNo)];
      if (activeMapping) {
        activeTestNo = activeMapping.test_no;
      }
      
      tests = updatedTests;
      renderTestSelectors();
    }

    function reindexQuestionsWithoutSave() {
      sortAndRenameTests();
      updateAllQuestionsCommonStemFromStems();
      // Only auto-reindex if auto-numbering is enabled
      const autoReindexEnabled = document.getElementById('toggle-auto-numbering').checked;
      if (autoReindexEnabled) {
        const uniqueTests = [...new Set(questions.map(q => q.test_no))];
        uniqueTests.forEach(tNo => {
          const testQs = questions.filter(q => q.test_no === tNo);
          testQs.sort((a, b) => {
            if (a.page_idx !== b.page_idx) return a.page_idx - b.page_idx;
            
            const page = pagesMap[a.page_idx];
            const mid = page.width / 2;
            
            // Classify columns: 0 = left, 1 = right, -1 = full-width
            const getCol = (q) => {
              const isLeft = q.crop_right <= mid + 40;
              const isRight = q.crop_left >= mid - 40;
              if (isLeft && !isRight) return 0;
              if (isRight && !isLeft) return 1;
              return -1;
            };
            
            const colA = getCol(a);
            const colB = getCol(b);
            
            if (colA !== colB) {
              if (colA === -1 || colB === -1) {
                return a.crop_top - b.crop_top;
              }
              return colA - colB;
            }
            
            return a.crop_top - b.crop_top;
          });
          testQs.forEach((q, idx) => {
            q.soru_no = idx + 1;
          });
        });
      }
      
      renderQuestionsList();
      renderCanvasBoxes();
      updatePageTestButtons();
    }
    
    function renderQuestionsList() {
      const container = document.getElementById('question-list-container');
      container.innerHTML = '';
      
      const title = document.createElement('div');
      title.className = 'section-title';
      title.textContent = 'Sorular & Kökler';
      container.appendChild(title);
      
      // Current test questions
      const testQs = questions.filter(q => q.test_no === activeTestNo);
      testQs.sort((a, b) => a.soru_no - b.soru_no);
      testQs.forEach(q => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'question-item-btn';
        if (selectedBoxId === q.tempId) btn.classList.add('selected-item');
        btn.onclick = () => selectAndScrollToBox(q.tempId);
        btn.innerHTML = `<span>Soru ${String(q.soru_no).padStart(2, '0')}</span><span class="badge">S. ${q.page_idx + 1}</span>`;
        container.appendChild(btn);
      });
      
      // Current test stems
      const testStems = stems.filter(s => s.test_no === activeTestNo);
      testStems.forEach(s => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'question-item-btn stem-item';
        if (selectedBoxId === s.tempId) btn.classList.add('selected-item');
        btn.onclick = () => selectAndScrollToBox(s.tempId);
        btn.innerHTML = `<span>Ortak Kök (${s.targetTempIds.map(tid => {
          let found = questions.find(q => q.tempId === tid);
          return found ? String(found.soru_no).padStart(2, '0') : '';
        }).filter(v => v).join(', ')})</span><span class="badge" style="background:#a855f7; color:white;">S. ${s.page_idx + 1}</span>`;
        container.appendChild(btn);
      });
    }
    
    function selectAndScrollToBox(tempId) {
      selectBox(tempId);
      const boxEl = document.getElementById('box_' + tempId);
      if (boxEl) {
        boxEl.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }
    
    function selectBox(tempId) {
      selectedBoxId = tempId;
      document.querySelectorAll('.crop-box').forEach(el => el.classList.remove('selected'));
      const el = document.getElementById('box_' + tempId);
      if (el) el.classList.add('selected');
      
      renderQuestionsList();
      showPropertiesPanel(tempId);
    }
    
    function showPropertiesPanel(tempId) {
      const accProp = document.getElementById('acc-properties');
      if (accProp) accProp.style.display = 'block';
      const accTitleText = document.getElementById('acc-prop-title-text');

      const panel = document.getElementById('properties-panel');
      panel.style.display = 'block';
      
      const testSel = document.getElementById('prop-test-select');
      
      if (tempId.startsWith('q_')) {
        const q = questions.find(item => item.tempId === tempId);
        const title = `Soru ${String(q.soru_no).padStart(2, '0')} Özellikleri`;
        document.getElementById('prop-title').textContent = title;
        if (accTitleText) accTitleText.textContent = title;
        testSel.value = q.test_no;
        
        // Show Soru No row and update input value
        document.getElementById('prop-soru-no-row').style.display = 'block';
        document.getElementById('prop-soru-no-input').value = q.soru_no;
        
        // Links to stems
        document.getElementById('prop-stem-link-row').style.display = 'block';
        document.getElementById('prop-stem-targets-row').style.display = 'none';
        document.getElementById('prop-stem-placement-row').style.display = 'none';
        
        const stemSel = document.getElementById('prop-stem-select');
        stemSel.innerHTML = '<option value="">(Yok)</option>';
        
        const availableStems = stems.filter(s => s.test_no === q.test_no && s.page_idx === q.page_idx);
        availableStems.forEach(s => {
          const opt = document.createElement('option');
          opt.value = s.tempId;
          opt.textContent = `Ortak Kök (${s.tempId})`;
          let isLinked = s.targetTempIds.includes(tempId);
          opt.selected = isLinked;
          stemSel.appendChild(opt);
        });

        document.getElementById('prop-subcrops-section').style.display = 'block';
        renderPropSubcropsList(q);
      } else if (tempId.startsWith('sub_')) {
        let parentQ = null;
        for (let q of questions) {
          if (q.sub_crops) {
            let found = q.sub_crops.find(sub => sub.tempId === tempId);
            if (found) {
              parentQ = q;
              break;
            }
          }
        }
        if (parentQ) {
          const title = `Soru ${String(parentQ.soru_no).padStart(2, '0')} Alt Parça`;
          document.getElementById('prop-title').textContent = title + " Özellikleri";
          const accTitleText = document.getElementById('acc-prop-title-text');
          if (accTitleText) accTitleText.textContent = title;
          testSel.value = parentQ.test_no;
          document.getElementById('prop-soru-no-row').style.display = 'none';
          document.getElementById('prop-stem-link-row').style.display = 'none';
          document.getElementById('prop-stem-targets-row').style.display = 'none';
          document.getElementById('prop-stem-placement-row').style.display = 'none';
          
          document.getElementById('prop-subcrops-section').style.display = 'block';
          renderPropSubcropsList(parentQ, tempId);
        }
      } else {
        // Stem selected
        const s = stems.find(item => item.tempId === tempId);
        const title = `Ortak Kök (${s.tempId})`;
        document.getElementById('prop-title').textContent = title + " Özellikleri";
        const accTitleText = document.getElementById('acc-prop-title-text');
        if (accTitleText) accTitleText.textContent = title;
        testSel.value = s.test_no;
        document.getElementById('prop-soru-no-row').style.display = 'none';
        document.getElementById('prop-stem-link-row').style.display = 'none';
        document.getElementById('prop-stem-targets-row').style.display = 'block';
        document.getElementById('prop-stem-placement-row').style.display = 'block';
        document.getElementById('prop-stem-placement').value = s.placement || 'top';
        
        const listDiv = document.getElementById('prop-stem-checkboxes');
        listDiv.innerHTML = '';
        
        const testQs = questions.filter(q => q.test_no === s.test_no);
        testQs.forEach(q => {
          const lbl = document.createElement('label');
          const chk = document.createElement('input');
          chk.type = 'checkbox';
          chk.value = q.tempId;
          chk.checked = s.targetTempIds.includes(q.tempId);
          chk.onchange = () => toggleStemTarget(s.tempId, q.tempId, chk.checked);
          lbl.appendChild(chk);
          lbl.appendChild(document.createTextNode(` Soru ${String(q.soru_no).padStart(2, '0')}`));
          listDiv.appendChild(lbl);
        });

        document.getElementById('prop-subcrops-section').style.display = 'none';
      }
      openAccordion('acc-properties');
    }

    function changeSelectedStemPlacement(placement) {
      if (!selectedBoxId || selectedBoxId.startsWith('q_')) return;
      const s = stems.find(item => item.tempId === selectedBoxId);
      if (s) {
        modifiedPages.add(s.page_idx);
        s.placement = placement;
        reindexQuestions();
      }
    }
    
    function hidePropertiesPanel() {
      const accProp = document.getElementById('acc-properties');
      if (accProp) {
        accProp.style.display = 'none';
        accProp.classList.remove('active');
      }
      document.getElementById('properties-panel').style.display = 'none';
      toggleAccordion('acc-questions');
    }
    
    // Checkbox toggling for target questions linked to common stem
    function toggleStemTarget(stemId, qId, checked) {
      const s = stems.find(item => item.tempId === stemId);
      if (!s) return;
      modifiedPages.add(s.page_idx);
      if (checked) {
        if (!s.targetTempIds.includes(qId)) s.targetTempIds.push(qId);
      } else {
        s.targetTempIds = s.targetTempIds.filter(tid => tid !== qId);
      }
      reindexQuestions();
    }
    
    function linkQuestionToStem(stemId) {
      if (!selectedBoxId || !selectedBoxId.startsWith('q_')) return;
      // Remove this question from all other stems on the page
      const q = questions.find(item => item.tempId === selectedBoxId);
      if (q) modifiedPages.add(q.page_idx);
      stems.forEach(s => {
        s.targetTempIds = s.targetTempIds.filter(tid => tid !== selectedBoxId);
      });
      // Link to selected stem
      if (stemId) {
        const s = stems.find(item => item.tempId === stemId);
        if (s && !s.targetTempIds.includes(selectedBoxId)) {
          s.targetTempIds.push(selectedBoxId);
        }
      }
      reindexQuestions();
    }
    
    function changeSelectedBoxTest(testNo) {
      testNo = parseInt(testNo);
      if (!selectedBoxId) return;
      
      const testObj = tests.find(t => t.test_no === testNo);
      if (!testObj) return;
      
      if (selectedBoxId.startsWith('q_')) {
        const q = questions.find(item => item.tempId === selectedBoxId);
        if (q) {
          modifiedPages.add(q.page_idx);
          q.test_no = testNo;
          q.test_name = testObj.test_name;
          
          // Re-calculate default soru_no for this test if auto-numbering is disabled
          const autoReindexEnabled = document.getElementById('toggle-auto-numbering').checked;
          if (!autoReindexEnabled) {
            const testQsForAuto = questions.filter(item => item.test_no === testNo && item.tempId !== q.tempId);
            const maxSoruNo = testQsForAuto.reduce((max, item) => Math.max(max, item.soru_no || 0), 0);
            q.soru_no = maxSoruNo + 1;
          }
          
          // Switch active test of the editor to the new test!
          selectTest(testNo);
        }
        // Unlink from stem if test changes
        stems.forEach(s => {
          s.targetTempIds = s.targetTempIds.filter(tid => tid !== selectedBoxId);
        });
      } else {
        const s = stems.find(item => item.tempId === selectedBoxId);
        if (s) {
          modifiedPages.add(s.page_idx);
          s.test_no = testNo;
          s.targetTempIds.forEach(tid => {
            const q = questions.find(item => item.tempId === tid);
            if (q) {
              q.test_no = testNo;
              q.test_name = testObj.test_name;
            }
          });
          
          // Switch active test of the editor to the new test!
          selectTest(testNo);
        }
      }
      
      reindexQuestions();
      showPropertiesPanel(selectedBoxId);
    }

    function changeSelectedBoxSoruNo(val) {
      if (!selectedBoxId || !selectedBoxId.startsWith('q_')) return;
      const q = questions.find(item => item.tempId === selectedBoxId);
      if (q) {
        const newNo = parseInt(val);
        if (!isNaN(newNo) && newNo > 0) {
          modifiedPages.add(q.page_idx);
          q.soru_no = newNo;
          reindexQuestions();
          showPropertiesPanel(selectedBoxId);
        }
      }
    }
    
    async function deleteSelectedBox() {
      if (!selectedBoxId) return;
      const isConfirmed = await showConfirm("Bu kutuyu silmek istediğinize emin misiniz?");
      if (isConfirmed) {
        if (selectedBoxId.startsWith('q_')) {
          const qObj = questions.find(item => item.tempId === selectedBoxId);
          if (qObj) {
            modifiedPages.add(qObj.page_idx);
          }
          questions = questions.filter(item => item.tempId !== selectedBoxId);
          stems.forEach(s => {
            s.targetTempIds = s.targetTempIds.filter(tid => tid !== selectedBoxId);
          });
        } else {
          const sObj = stems.find(item => item.tempId === selectedBoxId);
          if (sObj) {
            modifiedPages.add(sObj.page_idx);
            // Clear common_stem reference of any question linked to this stem
            questions.forEach(q => {
              if (sObj.targetTempIds.includes(q.tempId)) {
                q.common_stem = null;
              }
            });
          }
          stems = stems.filter(item => item.tempId !== selectedBoxId);
        }
        
        selectedBoxId = null;
        hidePropertiesPanel();
        reindexQuestions();
      }
    }
    
    function renderPages() {
      const container = document.getElementById('canvas-container');
      container.innerHTML = '';
      
      pages.forEach(p => {
        const pWrap = document.createElement('div');
        pWrap.className = 'page-wrapper';
        pWrap.id = 'page_wrap_' + p.page_idx;
        pWrap.style.width = '800px'; 
        const scaleHeight = 800 * (p.height / p.width);
        pWrap.style.height = scaleHeight + 'px';
        
        const badge = document.createElement('div');
        badge.className = 'page-header-badge';
        badge.style.display = 'flex';
        badge.style.justifyContent = 'space-between';
        badge.style.alignItems = 'center';
        badge.style.gap = '15px';
        badge.style.width = 'auto';
        
        const titleSpan = document.createElement('span');
        titleSpan.textContent = `Sayfa ${p.page_idx + 1}`;
        badge.appendChild(titleSpan);
        
        const detectBtn = document.createElement('button');
        detectBtn.id = 'detect_btn_' + p.page_idx;
        detectBtn.textContent = '🤖 Otomatik Algıla';
        detectBtn.style.background = 'var(--accent)';
        detectBtn.style.color = 'white';
        detectBtn.style.border = 'none';
        detectBtn.style.padding = '2px 8px';
        detectBtn.style.borderRadius = '4px';
        detectBtn.style.cursor = 'pointer';
        detectBtn.style.fontSize = '0.75rem';
        detectBtn.style.fontWeight = 'bold';
        detectBtn.onclick = (e) => {
          e.stopPropagation();
          detectPageQuestions(p.page_idx);
        };
        badge.appendChild(detectBtn);

        const optPageBtn = document.createElement('button');
        optPageBtn.textContent = '🪄 Sayfayı Optimize Et';
        optPageBtn.style.background = '#3b82f6';
        optPageBtn.style.color = 'white';
        optPageBtn.style.border = 'none';
        optPageBtn.style.padding = '2px 8px';
        optPageBtn.style.borderRadius = '4px';
        optPageBtn.style.cursor = 'pointer';
        optPageBtn.style.fontSize = '0.75rem';
        optPageBtn.style.fontWeight = 'bold';
        optPageBtn.style.marginLeft = '8px';
        optPageBtn.onclick = (e) => {
          e.stopPropagation();
          optimizePageQuestions(p.page_idx);
        };
        badge.appendChild(optPageBtn);

        const splitTestBtn = document.createElement('button');
        splitTestBtn.textContent = '✂️ Test Böl/Oluştur';
        splitTestBtn.style.background = '#8b5cf6';
        splitTestBtn.style.color = 'white';
        splitTestBtn.style.border = 'none';
        splitTestBtn.style.padding = '2px 8px';
        splitTestBtn.style.borderRadius = '4px';
        splitTestBtn.style.cursor = 'pointer';
        splitTestBtn.style.fontSize = '0.75rem';
        splitTestBtn.style.fontWeight = 'bold';
        splitTestBtn.style.marginLeft = '8px';
        splitTestBtn.onclick = (e) => {
          e.stopPropagation();
          openSplitModal(p.page_idx);
        };
        badge.appendChild(splitTestBtn);

        pWrap.appendChild(badge);
        
        const img = document.createElement('img');
        img.className = 'page-image';
        img.src = p.image_url;
        img.alt = `Sayfa ${p.page_idx + 1}`;
        img.loading = p === pages[0] ? 'eager' : 'lazy';
        img.decoding = 'async';
        img.fetchPriority = p === pages[0] ? 'high' : 'low';
        img.onload = img.onerror = () => {
          if (p.page_idx === 0) {
            isFirstPageLoaded = true;
          }
        };
        pWrap.appendChild(img);
        
        const overlay = document.createElement('div');
        overlay.className = 'drawing-overlay';
        overlay.id = 'overlay_' + p.page_idx;
        overlay.onpointerdown = (e) => handleMouseDown(e, p.page_idx, p.width, p.height);
        overlay.onmousemove = (e) => {
          if (activeTool !== 'select') {
            updateCrosshairs(e, pWrap);
          } else {
            hideCrosshairs();
          }
        };
        overlay.onmouseleave = () => {
          hideCrosshairs();
        };
        pWrap.appendChild(overlay);
        
        container.appendChild(pWrap);
        updateDetectButtonText(p.page_idx);
        setTimeout(() => renderPageMarkers(p.page_idx), 0);
      });
    }
    
    function renderCanvasBoxes() {
      document.querySelectorAll('.crop-box').forEach(el => el.remove());
      
      const activeQs = questions.filter(q => q.test_no === activeTestNo);
      activeQs.forEach(q => {
        const wrap = document.getElementById('page_wrap_' + q.page_idx);
        if (!wrap) return;
        const page = pagesMap[q.page_idx];
        
        const box = document.createElement('div');
        box.className = 'crop-box';
        box.id = 'box_' + q.tempId;
        if (selectedBoxId === q.tempId) box.classList.add('selected');
        
        let leftVal = q.crop_left;
        let rightVal = q.crop_right;
        const hideNumbers = document.getElementById('toggle-hide-numbers').checked;
        if (hideNumbers && q.anchor_right !== null && q.anchor_right !== undefined) {
          leftVal = q.anchor_right;
          if (leftVal >= rightVal) leftVal = q.crop_left;
        }
        
        box.style.left = (leftVal / page.width) * 100 + '%';
        box.style.top = (q.crop_top / page.height) * 100 + '%';
        box.style.width = ((rightVal - leftVal) / page.width) * 100 + '%';
        box.style.height = ((q.crop_bottom - q.crop_top) / page.height) * 100 + '%';
        
        box.onpointerdown = (e) => {
          e.stopPropagation();
          if (activeTool === 'select') {
            selectBox(q.tempId);
            startDragging(e, q.tempId, q.page_idx);
          }
        };
        
        const label = document.createElement('div');
        label.className = 'box-label';
        label.textContent = `Soru ${String(q.soru_no).padStart(2, '0')}`;
        box.appendChild(label);
        
        const delBtn = document.createElement('button');
        delBtn.className = 'box-delete';
        delBtn.textContent = '×';
        delBtn.onpointerdown = (e) => e.stopPropagation();
        delBtn.onclick = (e) => {
          e.stopPropagation();
          selectBox(q.tempId);
          deleteSelectedBox();
        };
        box.appendChild(delBtn);
        
        const expandBtn = document.createElement('button');
        expandBtn.className = 'box-action-btn expand';
        expandBtn.textContent = '+';
        expandBtn.title = 'Kutuyu Genişlet (Akıllı)';
        expandBtn.onpointerdown = (e) => e.stopPropagation();
        expandBtn.onclick = (e) => {
          e.stopPropagation();
          selectBox(q.tempId);
          adjustBoxBoundsSmart(q.tempId, 'expand');
        };
        box.appendChild(expandBtn);
        
        const shrinkBtn = document.createElement('button');
        shrinkBtn.className = 'box-action-btn shrink';
        shrinkBtn.textContent = '-';
        shrinkBtn.title = 'Kutuyu Daralt / Metne Hizala';
        shrinkBtn.onpointerdown = (e) => e.stopPropagation();
        shrinkBtn.onclick = (e) => {
          e.stopPropagation();
          selectBox(q.tempId);
          adjustBoxBoundsSmart(q.tempId, 'shrink');
        };
        box.appendChild(shrinkBtn);
        
        const handles = ['n', 's', 'e', 'w', 'nw', 'ne', 'sw', 'se'];
        handles.forEach(h => {
          const hEl = document.createElement('div');
          hEl.className = `handle handle-${h}`;
          hEl.onpointerdown = (e) => {
            e.stopPropagation();
            startResizing(e, q.tempId, q.page_idx, h);
          };
          box.appendChild(hEl);
        });
        
        wrap.appendChild(box);

        // Render sub crops if exist
        if (q.sub_crops) {
          q.sub_crops.forEach((sub, subIdx) => {
            const subWrap = document.getElementById('page_wrap_' + sub.page_idx);
            if (!subWrap) return;
            const subPage = pagesMap[sub.page_idx];
            
            const subBox = document.createElement('div');
            subBox.className = 'crop-box sub-crop-box';
            subBox.id = 'box_' + sub.tempId;
            if (selectedBoxId === sub.tempId) subBox.classList.add('selected');
            
            subBox.style.border = '2px dashed #10b981';
            subBox.style.background = 'rgba(16, 185, 129, 0.15)';
            subBox.style.left = (sub.crop_left / subPage.width) * 100 + '%';
            subBox.style.top = (sub.crop_top / subPage.height) * 100 + '%';
            subBox.style.width = ((sub.crop_right - sub.crop_left) / subPage.width) * 100 + '%';
            subBox.style.height = ((sub.crop_bottom - sub.crop_top) / subPage.height) * 100 + '%';
            
            subBox.onpointerdown = (e) => {
              e.stopPropagation();
              if (activeTool === 'select') {
                selectBox(sub.tempId);
                startDragging(e, sub.tempId, sub.page_idx);
              }
            };
            
            const subLabel = document.createElement('div');
            subLabel.className = 'box-label';
            subLabel.style.borderColor = '#10b981';
            subLabel.textContent = `Soru ${String(q.soru_no).padStart(2, '0')} (Parça ${subIdx + 1})`;
            subBox.appendChild(subLabel);
            
            const subDelBtn = document.createElement('button');
            subDelBtn.className = 'box-delete';
            subDelBtn.textContent = '×';
            subDelBtn.onpointerdown = (e) => e.stopPropagation();
            subDelBtn.onclick = (e) => {
              e.stopPropagation();
              deleteSubCrop(q.tempId, sub.tempId);
            };
            subBox.appendChild(subDelBtn);

            const subExpandBtn = document.createElement('button');
            subExpandBtn.className = 'box-action-btn expand';
            subExpandBtn.textContent = '+';
            subExpandBtn.title = 'Parçayı Genişlet (Akıllı)';
            subExpandBtn.onpointerdown = (e) => e.stopPropagation();
            subExpandBtn.onclick = (e) => {
              e.stopPropagation();
              selectBox(sub.tempId);
              adjustBoxBoundsSmart(sub.tempId, 'expand');
            };
            subBox.appendChild(subExpandBtn);
            
            const subShrinkBtn = document.createElement('button');
            subShrinkBtn.className = 'box-action-btn shrink';
            subShrinkBtn.textContent = '-';
            subShrinkBtn.title = 'Parçayı Daralt / Metne Hizala';
            subShrinkBtn.onpointerdown = (e) => e.stopPropagation();
            subShrinkBtn.onclick = (e) => {
              e.stopPropagation();
              selectBox(sub.tempId);
              adjustBoxBoundsSmart(sub.tempId, 'shrink');
            };
            subBox.appendChild(subShrinkBtn);
            
            const subHandles = ['n', 's', 'e', 'w', 'nw', 'ne', 'sw', 'se'];
            subHandles.forEach(h => {
              const hEl = document.createElement('div');
              hEl.className = `handle handle-${h}`;
              hEl.style.borderColor = '#10b981';
              hEl.onpointerdown = (e) => {
                e.stopPropagation();
                startResizing(e, sub.tempId, sub.page_idx, h);
              };
              subBox.appendChild(hEl);
            });
            
            subWrap.appendChild(subBox);
          });
        }
      });
      
      const activeStems = stems.filter(s => s.test_no === activeTestNo);
      activeStems.forEach(s => {
        const wrap = document.getElementById('page_wrap_' + s.page_idx);
        if (!wrap) return;
        const page = pagesMap[s.page_idx];
        
        const box = document.createElement('div');
        box.className = 'crop-box stem-box';
        box.id = 'box_' + s.tempId;
        if (selectedBoxId === s.tempId) box.classList.add('selected');
        
        box.style.left = (s.crop_left / page.width) * 100 + '%';
        box.style.top = (s.crop_top / page.height) * 100 + '%';
        box.style.width = ((s.crop_right - s.crop_left) / page.width) * 100 + '%';
        box.style.height = ((s.crop_bottom - s.crop_top) / page.height) * 100 + '%';
        
        box.onpointerdown = (e) => {
          e.stopPropagation();
          if (activeTool === 'select') {
            selectBox(s.tempId);
            startDragging(e, s.tempId, s.page_idx);
          }
        };
        
        const label = document.createElement('div');
        label.className = 'box-label';
        const linkedNos = s.targetTempIds.map(tid => {
          let found = questions.find(q => q.tempId === tid);
          return found ? String(found.soru_no).padStart(2, '0') : '';
        }).filter(v => v).sort();
        label.textContent = `Ortak Kök` + (linkedNos.length > 0 ? ` (Soru ${linkedNos.join(', ')})` : '');
        box.appendChild(label);
        
        const delBtn = document.createElement('button');
        delBtn.className = 'box-delete';
        delBtn.textContent = '×';
        delBtn.onpointerdown = (e) => e.stopPropagation();
        delBtn.onclick = (e) => {
          e.stopPropagation();
          selectBox(s.tempId);
          deleteSelectedBox();
        };
        box.appendChild(delBtn);
        
        const expandBtn = document.createElement('button');
        expandBtn.className = 'box-action-btn expand';
        expandBtn.textContent = '+';
        expandBtn.title = 'Kökü Genişlet (Akıllı)';
        expandBtn.onpointerdown = (e) => e.stopPropagation();
        expandBtn.onclick = (e) => {
          e.stopPropagation();
          selectBox(s.tempId);
          adjustBoxBoundsSmart(s.tempId, 'expand');
        };
        box.appendChild(expandBtn);
        
        const shrinkBtn = document.createElement('button');
        shrinkBtn.className = 'box-action-btn shrink';
        shrinkBtn.textContent = '-';
        shrinkBtn.title = 'Kökü Daralt / Metne Hizala';
        shrinkBtn.onpointerdown = (e) => e.stopPropagation();
        shrinkBtn.onclick = (e) => {
          e.stopPropagation();
          selectBox(s.tempId);
          adjustBoxBoundsSmart(s.tempId, 'shrink');
        };
        box.appendChild(shrinkBtn);
        
        const handles = ['n', 's', 'e', 'w', 'nw', 'ne', 'sw', 'se'];
        handles.forEach(h => {
          const hEl = document.createElement('div');
          hEl.className = `handle handle-${h}`;
          hEl.onpointerdown = (e) => {
            e.stopPropagation();
            startResizing(e, s.tempId, s.page_idx, h);
          };
          box.appendChild(hEl);
        });
        
        wrap.appendChild(box);
      });
    }
    
    function startDragging(e, tempId, pageIdx) {
      isDragging = true;
      trackingBoxId = tempId;
      dragStartMouse = { x: e.clientX, y: e.clientY };
      
      let box = null;
      if (tempId.startsWith('q_')) {
        box = questions.find(q => q.tempId === tempId);
      } else if (tempId.startsWith('s_')) {
        box = stems.find(s => s.tempId === tempId);
      } else if (tempId.startsWith('sub_')) {
        for (let q of questions) {
          if (q.sub_crops) {
            let found = q.sub_crops.find(sub => sub.tempId === tempId);
            if (found) {
              box = found;
              break;
            }
          }
        }
      }
      if (box) {
        modifiedPages.add(box.page_idx);
      }
      
      let leftVal = box.crop_left;
      const hideNumbers = document.getElementById('toggle-hide-numbers').checked;
      if (hideNumbers && box.anchor_right !== null && box.anchor_right !== undefined) {
        leftVal = box.anchor_right;
        if (leftVal >= box.crop_right) leftVal = box.crop_left;
      }

      dragStartBox = {
        left: leftVal,
        top: box.crop_top,
        width: box.crop_right - leftVal,
        height: box.crop_bottom - box.crop_top,
        crop_left: box.crop_left,
        crop_right: box.crop_right
      };
      
      document.addEventListener('pointermove', handleMouseMove);
      document.addEventListener('pointerup', handleMouseUp);
    }
    
    function startResizing(e, tempId, pageIdx, handle) {
      isResizing = true;
      trackingBoxId = tempId;
      resizeHandle = handle;
      dragStartMouse = { x: e.clientX, y: e.clientY };
      
      let box = null;
      if (tempId.startsWith('q_')) {
        box = questions.find(q => q.tempId === tempId);
      } else if (tempId.startsWith('s_')) {
        box = stems.find(s => s.tempId === tempId);
      } else if (tempId.startsWith('sub_')) {
        for (let q of questions) {
          if (q.sub_crops) {
            let found = q.sub_crops.find(sub => sub.tempId === tempId);
            if (found) {
              box = found;
              break;
            }
          }
        }
      }
      if (box) {
        modifiedPages.add(box.page_idx);
      }
      
      let leftVal = box.crop_left;
      const hideNumbers = document.getElementById('toggle-hide-numbers').checked;
      if (hideNumbers && box.anchor_right !== null && box.anchor_right !== undefined) {
        leftVal = box.anchor_right;
        if (leftVal >= box.crop_right) leftVal = box.crop_left;
      }

      dragStartBox = {
        left: leftVal,
        top: box.crop_top,
        width: box.crop_right - leftVal,
        height: box.crop_bottom - box.crop_top
      };
      
      document.addEventListener('pointermove', handleMouseMove);
      document.addEventListener('pointerup', handleMouseUp);
    }
    
    function handleMouseMove(e) {
      if (!trackingBoxId) return;
      let box = null;
      if (trackingBoxId.startsWith('q_')) {
        box = questions.find(q => q.tempId === trackingBoxId);
      } else if (trackingBoxId.startsWith('s_')) {
        box = stems.find(s => s.tempId === trackingBoxId);
      } else if (trackingBoxId.startsWith('sub_')) {
        for (let q of questions) {
          if (q.sub_crops) {
            let found = q.sub_crops.find(sub => sub.tempId === trackingBoxId);
            if (found) {
              box = found;
              break;
            }
          }
        }
      }
      if (!box) return;
      const page = pagesMap[box.page_idx];
      if (!page) return;
      const wrap = document.getElementById('page_wrap_' + box.page_idx);
      if (!wrap) return;
      
      const wrapWidth = wrap.clientWidth;
      const ptsPerPixel = page.width / wrapWidth;
      const deltaX = (e.clientX - dragStartMouse.x) * ptsPerPixel;
      const deltaY = (e.clientY - dragStartMouse.y) * ptsPerPixel;
      
      const hideNumbers = document.getElementById('toggle-hide-numbers').checked;

      if (isDragging) {
        let newLeft = Math.max(0, Math.min(dragStartBox.left + deltaX, page.width - dragStartBox.width));
        let newTop = Math.max(0, Math.min(dragStartBox.top + deltaY, page.height - dragStartBox.height));
        
        if (hideNumbers && box.anchor_right !== null && box.anchor_right !== undefined) {
          let delta = newLeft - dragStartBox.left;
          box.crop_left = parseFloat((dragStartBox.crop_left + delta).toFixed(2));
          box.crop_right = parseFloat((dragStartBox.crop_right + delta).toFixed(2));
          box.anchor_right = parseFloat(newLeft.toFixed(2));
        } else {
          box.crop_left = parseFloat(newLeft.toFixed(2));
          box.crop_right = parseFloat((newLeft + dragStartBox.width).toFixed(2));
        }
        box.crop_top = parseFloat(newTop.toFixed(2));
        box.crop_bottom = parseFloat((newTop + dragStartBox.height).toFixed(2));
      } else if (isResizing) {
        let left = dragStartBox.left;
        let top = dragStartBox.top;
        let right = dragStartBox.left + dragStartBox.width;
        let bottom = dragStartBox.top + dragStartBox.height;
        
        if (resizeHandle.includes('e')) right = Math.max(left + 20, Math.min(right + deltaX, page.width));
        if (resizeHandle.includes('w')) left = Math.max(0, Math.min(left + deltaX, right - 20));
        if (resizeHandle.includes('s')) bottom = Math.max(top + 20, Math.min(bottom + deltaY, page.height));
        if (resizeHandle.includes('n')) top = Math.max(0, Math.min(top + deltaY, bottom - 20));
        
        if (hideNumbers && box.anchor_right !== null && box.anchor_right !== undefined) {
          if (resizeHandle.includes('w')) {
            box.anchor_right = parseFloat(left.toFixed(2));
          }
          box.crop_right = parseFloat(right.toFixed(2));
        } else {
          box.crop_left = parseFloat(left.toFixed(2));
          box.crop_right = parseFloat(right.toFixed(2));
        }
        box.crop_top = parseFloat(top.toFixed(2));
        box.crop_bottom = parseFloat(bottom.toFixed(2));
      }
      
      const domBox = document.getElementById('box_' + trackingBoxId);
      if (domBox) {
        let activeLeft = box.crop_left;
        let activeRight = box.crop_right;
        if (hideNumbers && box.anchor_right !== null && box.anchor_right !== undefined) {
          activeLeft = box.anchor_right;
          if (activeLeft >= activeRight) activeLeft = box.crop_left;
        }
        domBox.style.left = (activeLeft / page.width) * 100 + '%';
        domBox.style.top = (box.crop_top / page.height) * 100 + '%';
        domBox.style.width = ((activeRight - activeLeft) / page.width) * 100 + '%';
        domBox.style.height = ((box.crop_bottom - box.crop_top) / page.height) * 100 + '%';
      }
    }
    
    function handleMouseUp(e) {
      document.removeEventListener('pointermove', handleMouseMove);
      document.removeEventListener('pointerup', handleMouseUp);
      
      if (trackingBoxId) {
        let box = null;
        if (trackingBoxId.startsWith('q_')) {
          box = questions.find(q => q.tempId === trackingBoxId);
        } else if (trackingBoxId.startsWith('s_')) {
          box = stems.find(s => s.tempId === trackingBoxId);
        } else if (trackingBoxId.startsWith('sub_')) {
          for (let q of questions) {
            if (q.sub_crops) {
              let found = q.sub_crops.find(sub => sub.tempId === trackingBoxId);
              if (found) {
                box = found;
                break;
              }
            }
          }
        }
        if (box) {
          modifiedPages.add(box.page_idx);
        }
      }
      
      isDragging = false;
      isResizing = false;
      trackingBoxId = null;
      reindexQuestions();
    }
    
    function handleMouseDown(e, pageIdx, pageWidth, pageHeight) {
      if (activeTool === 'select') return;
      
      if (activeTool === 'add-marker') {
        const wrap = document.getElementById('page_wrap_' + pageIdx);
        const rect = wrap.getBoundingClientRect();
        const clickX = e.clientX - rect.left;
        const clickY = e.clientY - rect.top;
        
        const page = pagesMap[pageIdx];
        const scaleX = page.width / rect.width;
        const scaleY = page.height / rect.height;
        const pdfX = parseFloat((clickX * scaleX).toFixed(2));
        const pdfY = parseFloat((clickY * scaleY).toFixed(2));
        
        if (!pageMarkers[pageIdx]) {
          pageMarkers[pageIdx] = [];
        }
        pageMarkers[pageIdx].push({ x: pdfX, y: pdfY });
        
        renderPageMarkers(pageIdx);
        updateDetectButtonText(pageIdx);
        return;
      }

      if (activeTool === 'two-points') {
        const wrap = document.getElementById('page_wrap_' + pageIdx);
        const rect = wrap.getBoundingClientRect();
        const clickX = e.clientX - rect.left;
        const clickY = e.clientY - rect.top;
        
        const page = pagesMap[pageIdx];
        const scaleX = page.width / rect.width;
        const scaleY = page.height / rect.height;
        const pdfX = parseFloat((clickX * scaleX).toFixed(2));
        const pdfY = parseFloat((clickY * scaleY).toFixed(2));
        
        if (!twoPointClicks[pageIdx]) {
          // First point click
          twoPointClicks[pageIdx] = { x: pdfX, y: pdfY };
          
          // Render temp marker
          const tempMarker = document.createElement('div');
          tempMarker.className = 'two-point-temp-marker';
          tempMarker.id = 'two-point-temp-marker-' + pageIdx;
          tempMarker.style.left = (clickX / rect.width) * 100 + '%';
          tempMarker.style.top = (clickY / rect.height) * 100 + '%';
          wrap.appendChild(tempMarker);
        } else {
          // Second point click - create the box!
          const p1 = twoPointClicks[pageIdx];
          const left = Math.min(p1.x, pdfX);
          const top = Math.min(p1.y, pdfY);
          const right = Math.max(p1.x, pdfX);
          const bottom = Math.max(p1.y, pdfY);
          
          if (right - left > 5 && bottom - top > 5) {
            modifiedPages.add(pageIdx);
            const activeTestObj = tests.find(t => t.test_no === activeTestNo);
            const testNameVal = activeTestObj ? activeTestObj.test_name : 'Test-' + activeTestNo;
            
            const testQsForAuto = questions.filter(q => q.test_no === activeTestNo);
            const maxSoruNo = testQsForAuto.reduce((max, q) => Math.max(max, q.soru_no || 0), 0);
            const initialSoruNo = maxSoruNo + 1;

            const newQ = {
              tempId: 'q_' + questions.length + '_' + Math.floor(Math.random() * 100000),
              test_no: activeTestNo,
              test_name: testNameVal,
              soru_no: initialSoruNo,
              page_idx: pageIdx,
              crop_left: parseFloat(left.toFixed(2)),
              crop_top: parseFloat(top.toFixed(2)),
              crop_right: parseFloat(right.toFixed(2)),
              crop_bottom: parseFloat(bottom.toFixed(2)),
              common_stem: null,
              sub_crops: []
            };
            
            questions.push(newQ);
            selectedBoxId = newQ.tempId;
            
            reindexQuestions();
            renderCanvasBoxes();
            renderQuestionsList();
          }
          
          // Clear temp marker and click data
          const tempMarker = document.getElementById('two-point-temp-marker-' + pageIdx);
          if (tempMarker) tempMarker.remove();
          delete twoPointClicks[pageIdx];
        }
        return;
      }
      
      isDrawing = true;
      drawActivePageIdx = pageIdx;
      modifiedPages.add(pageIdx);
      
      const wrap = document.getElementById('page_wrap_' + pageIdx);
      const rect = wrap.getBoundingClientRect();
      drawStart = {
        x: e.clientX - rect.left,
        y: e.clientY - rect.top
      };
      
      const tempRect = document.createElement('div');
      tempRect.className = 'temp-rect';
      tempRect.id = 'temp-rect';
      tempRect.style.left = drawStart.x + 'px';
      tempRect.style.top = drawStart.y + 'px';
      wrap.appendChild(tempRect);
      
      document.addEventListener('pointermove', handleDrawMouseMove);
      document.addEventListener('pointerup', handleDrawMouseUp);
    }
    
    function handleDrawMouseMove(e) {
      if (!isDrawing) return;
      const wrap = document.getElementById('page_wrap_' + drawActivePageIdx);
      const rect = wrap.getBoundingClientRect();
      
      const currentX = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
      const currentY = Math.max(0, Math.min(e.clientY - rect.top, rect.height));
      
      const left = Math.min(drawStart.x, currentX);
      const top = Math.min(drawStart.y, currentY);
      const width = Math.abs(drawStart.x - currentX);
      const height = Math.abs(drawStart.y - currentY);
      
      const tempRect = document.getElementById('temp-rect');
      if (tempRect) {
        tempRect.style.left = left + 'px';
        tempRect.style.top = top + 'px';
        tempRect.style.width = width + 'px';
        tempRect.style.height = height + 'px';
      }
    }
    
    function handleDrawMouseUp(e) {
      document.removeEventListener('pointermove', handleDrawMouseMove);
      document.removeEventListener('pointerup', handleDrawMouseUp);
      isDrawing = false;
      
      const tempRect = document.getElementById('temp-rect');
      if (!tempRect) {
        setTool('select');
        return;
      }
      
      const leftPx = parseFloat(tempRect.style.left);
      const topPx = parseFloat(tempRect.style.top);
      const widthPx = parseFloat(tempRect.style.width || 0);
      const heightPx = parseFloat(tempRect.style.height || 0);
      tempRect.remove();
      
      if (widthPx > 15 && heightPx > 15) {
        modifiedPages.add(drawActivePageIdx);
        const wrap = document.getElementById('page_wrap_' + drawActivePageIdx);
        const rect = wrap.getBoundingClientRect();
        const page = pagesMap[drawActivePageIdx];
        
        const scaleX = page.width / rect.width;
        const scaleY = page.height / rect.height;
        
        const cropLeft = leftPx * scaleX;
        const cropTop = topPx * scaleY;
        const cropRight = (leftPx + widthPx) * scaleX;
        const cropBottom = (topPx + heightPx) * scaleY;
        
        const activeTestObj = tests.find(t => t.test_no === activeTestNo);
        const testNameVal = activeTestObj ? activeTestObj.test_name : 'Test-' + activeTestNo;
        
        if (activeTool === 'draw-question') {
          const testQsForAuto = questions.filter(q => q.test_no === activeTestNo);
          const maxSoruNo = testQsForAuto.reduce((max, q) => Math.max(max, q.soru_no || 0), 0);
          const initialSoruNo = maxSoruNo + 1;

          const newQ = {
            tempId: 'q_' + questions.length + '_' + Math.floor(Math.random() * 100000),
            test_no: activeTestNo,
            test_name: testNameVal,
            soru_no: initialSoruNo,
            page_idx: drawActivePageIdx,
            crop_left: parseFloat(cropLeft.toFixed(2)),
            crop_top: parseFloat(cropTop.toFixed(2)),
            crop_right: parseFloat(cropRight.toFixed(2)),
            crop_bottom: parseFloat(cropBottom.toFixed(2)),
            common_stem: null,
            sub_crops: []
          };
          questions.push(newQ);
          selectedBoxId = newQ.tempId;
        } else if (activeTool === 'draw-stem') {
          const newStem = {
            tempId: 's_' + stems.length + '_' + Math.floor(Math.random() * 100000),
            test_no: activeTestNo,
            page_idx: drawActivePageIdx,
            crop_left: parseFloat(cropLeft.toFixed(2)),
            crop_top: parseFloat(cropTop.toFixed(2)),
            crop_right: parseFloat(cropRight.toFixed(2)),
            crop_bottom: parseFloat(cropBottom.toFixed(2)),
            placement: 'top',
            targetTempIds: []
          };
          stems.push(newStem);
          selectedBoxId = newStem.tempId;
        } else if (activeTool === 'draw-subcrop') {
          let q = null;
          if (selectedBoxId && selectedBoxId.startsWith('q_')) {
            q = questions.find(item => item.tempId === selectedBoxId);
          } else if (selectedBoxId && selectedBoxId.startsWith('sub_')) {
            for (let item of questions) {
              if (item.sub_crops && item.sub_crops.some(sub => sub.tempId === selectedBoxId)) {
                q = item;
                break;
              }
            }
          }
          
          if (q) {
            const newSub = {
              tempId: 'sub_' + Math.floor(Math.random() * 1000000),
              page_idx: drawActivePageIdx,
              crop_left: parseFloat(cropLeft.toFixed(2)),
              crop_top: parseFloat(cropTop.toFixed(2)),
              crop_right: parseFloat(cropRight.toFixed(2)),
              crop_bottom: parseFloat(cropBottom.toFixed(2)),
              placement: 'left'
            };
            q.sub_crops = q.sub_crops || [];
            q.sub_crops.push(newSub);
            selectedBoxId = newSub.tempId;
            modifiedPages.add(q.page_idx);
          }
        }
        
        reindexQuestions();
        showPropertiesPanel(selectedBoxId);
      }
      
      setTool('select');
    }
    
    function saveEdits() {
      reindexQuestionsWithoutSave();
      const btn = document.getElementById('btn-save-edits');
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="btn-spinner"></span> Hazırlanıyor...`;
        btn.style.opacity = '0.8';
        btn.style.cursor = 'wait';
      }

      
      const payloadQuestions = questions.map(q => {
        let stem = stems.find(s => s.test_no === q.test_no && s.targetTempIds.includes(q.tempId));
        let common_stem_data = null;
        if (stem) {
          common_stem_data = {
            page_idx: stem.page_idx,
            crop_left: stem.crop_left,
            crop_top: stem.crop_top,
            crop_right: stem.crop_right,
            crop_bottom: stem.crop_bottom,
            placement: stem.placement || 'top'
          };
        }
        return {
          test_no: q.test_no,
          test_name: q.test_name,
          soru_no: q.soru_no,
          page_idx: q.page_idx,
          crop_left: q.crop_left,
          crop_top: q.crop_top,
          crop_right: q.crop_right,
          crop_bottom: q.crop_bottom,
          anchor_right: q.anchor_right !== undefined ? q.anchor_right : null,
          common_stem: common_stem_data,
          sub_crops: (q.sub_crops || []).map(sub => ({
            page_idx: sub.page_idx,
            crop_left: sub.crop_left,
            crop_top: sub.crop_top,
            crop_right: sub.crop_right,
            crop_bottom: sub.crop_bottom,
            placement: sub.placement || 'left'
          }))
        };
      });
      
      const payload = {
        pdf_name: pdfName,
        questions: payloadQuestions,
        hide_question_numbers: document.getElementById('toggle-hide-numbers').checked,
        modified_pages: Array.from(modifiedPages)
      };
      
      fetch(`/jobs/${jobId}/easy-editor/save`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(payload)
      })
      .then(res => {
        if (!res.ok) throw new Error("Kayıt başarısız oldu.");
        return res.json();
      })
      .then(async (data) => {
        modifiedPages.clear();
        await showAlert("Değişiklikler başarıyla kaydedildi! ZIP indirmesi başlatılıyor...");
        const btn = document.getElementById('btn-save-edits');
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = "Tamamla ve İndir";
          btn.style.opacity = '1';
          btn.style.cursor = 'pointer';
        }
        const downloadUrl = `/jobs/${jobId}/download/all`;
        const downloadName = `${jobId}_test_pdfs.zip`;
        try {
          if (!await saveWithDesktopBridge(downloadUrl, downloadName)) {
            window.location.href = downloadUrl;
          }
        } catch (err) {
          await showAlert("PDF arşivi indirilemedi: " + err.message);
        }
      })
      .catch(async (err) => {
        const btn = document.getElementById('btn-save-edits');
        if (btn) {
          btn.disabled = false;
          btn.innerHTML = "Tamamla ve İndir";
          btn.style.opacity = '1';
          btn.style.cursor = 'pointer';
        }
        await showAlert("Hata: " + err.message);
      });
    }
    
    function showConfirm(message) {
      return new Promise((resolve) => {
        const modal = document.getElementById('custom-confirm-modal');
        const textEl = document.getElementById('custom-confirm-text');
        const okBtn = document.getElementById('custom-confirm-ok');
        const cancelBtn = document.getElementById('custom-confirm-cancel');
        
        textEl.textContent = message;
        modal.style.display = 'flex';
        modal.offsetHeight; // Force reflow
        modal.classList.add('show');
        
        function cleanup(val) {
          modal.classList.remove('show');
          setTimeout(() => {
            modal.style.display = 'none';
          }, 200);
          okBtn.onclick = null;
          cancelBtn.onclick = null;
          resolve(val);
        }
        
        okBtn.onclick = () => cleanup(true);
        cancelBtn.onclick = () => cleanup(false);
      });
    }

    function showAlert(message) {
      return new Promise((resolve) => {
        const modal = document.getElementById('custom-alert-modal');
        const textEl = document.getElementById('custom-alert-text');
        const okBtn = document.getElementById('custom-alert-ok');
        
        textEl.textContent = message;
        modal.style.display = 'flex';
        modal.offsetHeight; // Force reflow
        modal.classList.add('show');
        
        function cleanup() {
          modal.classList.remove('show');
          setTimeout(() => {
            modal.style.display = 'none';
          }, 200);
          okBtn.onclick = null;
          resolve();
        }
        
        okBtn.onclick = () => cleanup();
      });
    }

    function showHelpModal() {
      const modal = document.getElementById('custom-help-modal');
      modal.style.display = 'flex';
      modal.offsetHeight; // Force reflow
      modal.classList.add('show');
    }

    function hideHelpModal() {
      const modal = document.getElementById('custom-help-modal');
      modal.classList.remove('show');
      setTimeout(() => {
        modal.style.display = 'none';
      }, 200);
    }
    
    function returnToReview() {
      window.location.href = `/jobs/${jobId}`;
    }

    function checkJobStatus() {
      fetch(`/jobs/${jobId}/status`)
      .then(res => res.json())
      .then(data => {
        const pct = data.progress_percent || 0;
        document.getElementById('progress-bar').style.width = pct + '%';
        document.getElementById('progress-text').textContent = pct + '%';
        
        let msg = data.message || '';
        if (data.current_question) {
          msg += ` (${data.current_question})`;
        }
        document.getElementById('status-text').textContent = msg;
        
        const consoleEl = document.getElementById('log-console');
        if (data.log_tail && consoleEl) {
          consoleEl.textContent = data.log_tail;
          consoleEl.scrollTop = consoleEl.scrollHeight;
        }
        
        lastJobStatus = data.status;
        if (data.status === 'queued') {
          document.getElementById('status-icon').textContent = '⏳';
          document.getElementById('status-title').textContent = 'PDF Yükleniyor...';
        } else if (data.status === 'rendering') {
          document.getElementById('status-icon').textContent = '🔄';
          document.getElementById('status-title').textContent = 'Sayfalar Hazırlanıyor...';
        } else if (data.status === 'processing') {
          document.getElementById('status-icon').textContent = '✂️';
          document.getElementById('status-title').textContent = 'PDF Kesiliyor...';
        }
        
        // Periodically check if manifest is ready, load questions
        if (isProcessing && !questionsLoaded) {
          reloadQuestionsFromServer();
          

        }
        
        if (data.status === 'success' || data.status === 'completed') {
          isProcessing = false;
          if (pollInterval) clearInterval(pollInterval);
          

          
          const isManualMode = serverData.workflow_mode === 'manual';
          document.getElementById('status-icon').textContent = isManualMode ? '✍️' : '✅';
          document.getElementById('status-title').textContent = isManualMode ? 'Manuel Kesim Modu' : 'İşlem Tamamlandı';
          document.getElementById('status-text').textContent = isManualMode
            ? 'Boş editör hazır. Manuel çizebilir veya sayfa bazında otomatik algılamayı kullanabilirsiniz.'
            : 'Sorular otomatik olarak tespit edildi. Düzenleyebilirsiniz.';
          document.getElementById('progress-container').style.display = 'none';
          document.getElementById('progress-text').style.display = 'none';
          
          // Enable save button
          const saveBtn = document.getElementById('btn-save-edits');
          if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.style.opacity = '1';
            saveBtn.style.cursor = 'pointer';
          }
          
          reloadQuestionsFromServer();
        } else if (data.status === 'error' || data.status === 'empty' || data.status === 'cancelled') {
          isProcessing = false;
          clearInterval(pollInterval);
          

          
          document.getElementById('status-icon').textContent = '❌';
          document.getElementById('status-title').textContent = 'Hata Oluştu';
          document.getElementById('status-text').textContent = data.error || 'İşlem başarısız oldu.';
          document.getElementById('status-banner').style.background = '#7f1d1d';
        }
      })
      .catch(err => console.error("Status check failed:", err));
    }

    function reloadQuestionsFromServer() {
      fetch(`/jobs/${jobId}/easy-editor/questions-json`)
      .then(res => res.json())
      .then(data => {
        if (data.questions && data.questions.length > 0) {
          // Merge questions: preserve questions of modified pages, replace others
          const mergedQuestions = [];
          
          // Keep local questions for modified pages
          questions.forEach(q => {
            if (modifiedPages.has(q.page_idx)) {
              mergedQuestions.push(q);
            }
          });
          
          // Add server questions for unmodified pages
          data.questions.forEach((q, idx) => {
            if (!modifiedPages.has(q.page_idx)) {
              const sub_crops = (q.sub_crops || []).map((sub, sidx) => ({
                ...sub,
                tempId: 'sub_reload_' + idx + '_' + sidx + '_' + Math.floor(Math.random() * 100000)
              }));
              mergedQuestions.push({
                ...q,
                tempId: 'q_' + idx + '_' + Math.floor(Math.random() * 100000),
                sub_crops: sub_crops
              });
            }
          });
          
          questions = mergedQuestions;
          questionsLoaded = true;
          
          // Rebuild tests dropdown dynamically
          const testsMap = {};
          questions.forEach(q => {
            if (q.test_no !== undefined && q.test_no !== null) {
              testsMap[q.test_no] = q.test_name || `Test ${q.test_no}`;
            }
          });
          tests.forEach(t => {
            if (!testsMap[t.test_no]) {
              testsMap[t.test_no] = t.test_name;
            }
          });
          tests = Object.keys(testsMap).map(k => ({
            test_no: parseInt(k),
            test_name: testsMap[k]
          })).sort((a, b) => a.test_no - b.test_no);
          
          renderTestSelectors();
          
          // Rebuild stems, preserving local stems for modified pages
          const mergedStems = [];
          stems.forEach(s => {
            if (modifiedPages.has(s.page_idx)) {
              mergedStems.push(s);
            }
          });
          
          questions.forEach(q => {
            if (!modifiedPages.has(q.page_idx) && q.common_stem) {
              let existing = mergedStems.find(s => 
                s.test_no === q.test_no &&
                s.page_idx === q.common_stem.page_idx &&
                Math.abs(s.crop_left - q.common_stem.crop_left) < 1 &&
                Math.abs(s.crop_top - q.common_stem.crop_top) < 1 &&
                Math.abs(s.crop_right - q.common_stem.crop_right) < 1 &&
                Math.abs(s.crop_bottom - q.common_stem.crop_bottom) < 1
              );
              if (existing) {
                if (!existing.targetTempIds.includes(q.tempId)) {
                  existing.targetTempIds.push(q.tempId);
                }
              } else {
                mergedStems.push({
                  tempId: 's_' + mergedStems.length + '_' + Math.floor(Math.random() * 100000),
                  test_no: q.test_no,
                  page_idx: q.common_stem.page_idx,
                  crop_left: q.common_stem.crop_left,
                  crop_top: q.common_stem.crop_top,
                  crop_right: q.common_stem.crop_right,
                  crop_bottom: q.common_stem.crop_bottom,
                  placement: q.common_stem.placement || 'top',
                  targetTempIds: [q.tempId]
                });
              }
            }
          });
          stems = mergedStems;
          
          reindexQuestions();
        }
      });
    }

    function renderPageMarkers(pageIdx) {
      const wrap = document.getElementById('page_wrap_' + pageIdx);
      if (!wrap) return;
      wrap.querySelectorAll('.crop-marker').forEach(el => el.remove());
      
      const markers = pageMarkers[pageIdx] || [];
      const page = pagesMap[pageIdx];
      
      markers.forEach((m, idx) => {
        const mEl = document.createElement('div');
        mEl.className = 'crop-marker';
        mEl.style.left = (m.x / page.width) * 100 + '%';
        mEl.style.top = (m.y / page.height) * 100 + '%';
        mEl.textContent = idx + 1;
        
        mEl.onpointerdown = (e) => {
          e.stopPropagation();
        };
        mEl.onclick = (e) => {
          e.stopPropagation();
          pageMarkers[pageIdx].splice(idx, 1);
          renderPageMarkers(pageIdx);
          updateDetectButtonText(pageIdx);
        };
        
        wrap.appendChild(mEl);
      });
    }

    function updateDetectButtonText(pageIdx) {
      const detectBtn = document.getElementById('detect_btn_' + pageIdx);
      if (!detectBtn) return;
      const markers = pageMarkers[pageIdx] || [];
      if (markers.length > 0) {
        detectBtn.textContent = '🤖 İşaretlerden Algıla';
        detectBtn.style.background = '#ef4444';
      } else {
        detectBtn.textContent = '🤖 Otomatik Algıla';
        detectBtn.style.background = 'var(--accent)';
      }
    }

    async function detectPageQuestions(pageIdx) {
      const markers = pageMarkers[pageIdx] || [];
      if (markers.length > 0) {
        const isConfirmed = await showConfirm(`Mevcut kutuları silmeden yerleştirdiğiniz ${markers.length} yeni işarete göre yeni soru(lar) algılanıp eklensin mi?`);
        if (isConfirmed) {
          modifiedPages.add(pageIdx);
          try {
            const activeTestObj = tests.find(t => t.test_no === activeTestNo);
            const testNameVal = activeTestObj ? activeTestObj.test_name : 'Test-' + activeTestNo;
            const payload = {
              page_idx: pageIdx,
              markers: markers,
              test_no: activeTestNo,
              test_name: testNameVal
            };
            const res = await fetch(`/jobs/${jobId}/easy-editor/detect-from-markers`, {
              method: 'POST',
              headers: {
                'Content-Type': 'application/json'
              },
              body: JSON.stringify(payload)
            });
            if (!res.ok) throw new Error("İşaretlerden algılama API hatası");
            const data = await res.json();
            
            if (data.questions) {
              data.questions.forEach((q, idx) => {
                const tempId = 'q_' + (questions.length + idx + Math.floor(Math.random() * 1000));
                questions.push({ ...q, tempId: tempId });
              });
            }
            
            pageMarkers[pageIdx] = [];
            renderPageMarkers(pageIdx);
            updateDetectButtonText(pageIdx);
            reindexQuestions();
          } catch (err) {
            console.error("Detect from markers error:", err);
            showAlert("İşaretlerden algılama yapılamadı: " + err.message);
          }
        }
      } else {
        const isConfirmed = await showConfirm("Bu sayfadaki tüm mevcut kutular silinecek ve orijinal otomatik tespit edilen kutular yüklenecek. Emin misiniz?");
        if (isConfirmed) {
          modifiedPages.add(pageIdx);
          const activeTestObj = tests.find(t => t.test_no === activeTestNo);
          const testNameVal = activeTestObj ? activeTestObj.test_name : 'Test-' + activeTestNo;
          fetch(`/jobs/${jobId}/easy-editor/original-questions?page_idx=${pageIdx}&force_reanalyze=true&test_no=${activeTestNo}&test_name=${encodeURIComponent(testNameVal)}`)
          .then(res => res.json())
          .then(data => {
            questions = questions.filter(q => q.page_idx !== pageIdx);
            stems = stems.filter(s => s.page_idx !== pageIdx);
            
            if (data.questions) {
              data.questions.forEach((q, idx) => {
                const tempId = 'q_' + (questions.length + idx + Math.floor(Math.random() * 1000));
                questions.push({ ...q, tempId: tempId });
              });
            }
            
            reindexQuestions();
          });
        }
      }
    }

    async function optimizeBoxBounds(tempId) {
      const q = questions.find(item => item.tempId === tempId);
      if (!q) return;
      
      try {
        const payload = {
          page_idx: q.page_idx,
          crop_left: q.crop_left,
          crop_top: q.crop_top,
          crop_right: q.crop_right,
          crop_bottom: q.crop_bottom,
          soru_no: q.soru_no
        };
        
        const res = await fetch(`/jobs/${jobId}/easy-editor/optimize-box`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify(payload)
        });
        
        if (!res.ok) throw new Error("Optimizasyon API hatası");
        
        const data = await res.json();
        modifiedPages.add(q.page_idx);
        
        q.crop_left = data.crop_left;
        q.crop_top = data.crop_top;
        q.crop_right = data.crop_right;
        q.crop_bottom = data.crop_bottom;
        q.anchor_right = data.anchor_right;
        
        reindexQuestions();
        if (selectedBoxId === tempId) {
          showPropertiesPanel(tempId);
        }
      } catch (err) {
        console.error("Optimize error:", err);
        showAlert("Kutu sınırları optimize edilemedi: " + err.message);
      }
    }

    async function optimizeStemBounds(tempId) {
      const s = stems.find(item => item.tempId === tempId);
      if (!s) return;
      
      try {
        const payload = {
          page_idx: s.page_idx,
          crop_left: s.crop_left,
          crop_top: s.crop_top,
          crop_right: s.crop_right,
          crop_bottom: s.crop_bottom,
          soru_no: 0
        };
        
        const res = await fetch(`/jobs/${jobId}/easy-editor/optimize-box`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify(payload)
        });
        
        if (!res.ok) throw new Error("Optimizasyon API hatası");
        
        const data = await res.json();
        modifiedPages.add(s.page_idx);
        
        s.crop_left = data.crop_left;
        s.crop_top = data.crop_top;
        s.crop_right = data.crop_right;
        s.crop_bottom = data.crop_bottom;
        
        reindexQuestions();
        if (selectedBoxId === tempId) {
          showPropertiesPanel(tempId);
        }
      } catch (err) {
        console.error("Optimize error:", err);
        showAlert("Ortak kök sınırları optimize edilemedi: " + err.message);
      }
    }

    function renderPropSubcropsList(q, selSubId = null) {
      const listDiv = document.getElementById('prop-subcrops-list');
      listDiv.innerHTML = '';
      
      const subCrops = q.sub_crops || [];
      if (subCrops.length === 0) {
        listDiv.innerHTML = '<div style="font-size:0.8rem; color:var(--text-muted); text-align:center;">Ekli parça yok.</div>';
        return;
      }
      
      subCrops.forEach((sub, idx) => {
        const itemDiv = document.createElement('div');
        itemDiv.style.display = 'flex';
        itemDiv.style.alignItems = 'center';
        itemDiv.style.gap = '8px';
        itemDiv.style.padding = '6px';
        itemDiv.style.background = 'var(--bg-dark)';
        itemDiv.style.borderRadius = '6px';
        itemDiv.style.border = '1px solid ' + (selSubId === sub.tempId ? '#10b981' : 'var(--border)');
        
        const label = document.createElement('span');
        label.style.fontSize = '0.8rem';
        label.style.flex = '1';
        label.textContent = `Parça ${idx + 1} (Sayfa ${sub.page_idx + 1})`;
        
        const placementSelect = document.createElement('select');
        placementSelect.style.width = '90px';
        placementSelect.style.padding = '4px';
        placementSelect.style.fontSize = '0.75rem';
        placementSelect.innerHTML = `
          <option value="left">Yatay (Sağ)</option>
          <option value="top">Dikey (Alt)</option>
        `;
        placementSelect.value = sub.placement || 'left';
        placementSelect.onchange = () => {
          sub.placement = placementSelect.value;
          saveState();
          reindexQuestions();
        };
        
        const delBtn = document.createElement('button');
        delBtn.className = 'btn btn-danger';
        delBtn.style.padding = '4px 8px';
        delBtn.style.fontSize = '0.75rem';
        delBtn.style.background = '#ef4444';
        delBtn.textContent = 'Sil';
        delBtn.onclick = () => deleteSubCrop(q.tempId, sub.tempId);
        
        itemDiv.appendChild(label);
        itemDiv.appendChild(placementSelect);
        itemDiv.appendChild(delBtn);
        listDiv.appendChild(itemDiv);
      });
    }

    function deleteSubCrop(qTempId, subTempId) {
      const q = questions.find(item => item.tempId === qTempId);
      if (q && q.sub_crops) {
        modifiedPages.add(q.page_idx);
        q.sub_crops = q.sub_crops.filter(sub => sub.tempId !== subTempId);
        if (selectedBoxId === subTempId) {
          selectedBoxId = q.tempId;
        }
        reindexQuestions();
      }
    }

    function startDrawSubcrop() {
      if (!selectedBoxId) return;
      setTool('draw-subcrop');
    }

    async function optimizeSubCropBounds(qTempId, subTempId) {
      const q = questions.find(item => item.tempId === qTempId);
      if (!q || !q.sub_crops) return;
      const sub = q.sub_crops.find(s => s.tempId === subTempId);
      if (!sub) return;
      
      try {
        const payload = {
          page_idx: sub.page_idx,
          crop_left: sub.crop_left,
          crop_top: sub.crop_top,
          crop_right: sub.crop_right,
          crop_bottom: sub.crop_bottom,
          soru_no: 0
        };
        
        const res = await fetch(`/jobs/${jobId}/easy-editor/optimize-box`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify(payload)
        });
        
        if (!res.ok) throw new Error("Optimizasyon API hatası");
        
        const data = await res.json();
        modifiedPages.add(sub.page_idx);
        
        sub.crop_left = data.crop_left;
        sub.crop_top = data.crop_top;
        sub.crop_right = data.crop_right;
        sub.crop_bottom = data.crop_bottom;
        
        reindexQuestions();
        if (selectedBoxId === subTempId) {
          showPropertiesPanel(subTempId);
        }
      } catch (err) {
        console.error("Optimize error:", err);
        showAlert("Alt parça sınırları optimize edilemedi: " + err.message);
      }
    }

    async function optimizePageQuestions(pageIdx) {
      const pageQs = questions.filter(q => q.page_idx === pageIdx && q.test_no === activeTestNo);
      const pageStems = stems.filter(s => s.page_idx === pageIdx && s.test_no === activeTestNo);
      
      const total = pageQs.length + pageStems.length;
      if (total === 0) {
        showAlert("Bu sayfada optimize edilecek kutu bulunamadı.");
        return;
      }
      
      const isConfirmed = await showConfirm(`Bu sayfadaki tüm ${total} kutunun sınırları otomatik olarak optimize edilecek. Emin misiniz?`);
      if (!isConfirmed) return;
      
      const statusTitle = document.getElementById('status-title');
      const statusText = document.getElementById('status-text');
      const prevTitle = statusTitle.textContent;
      const prevText = statusText.textContent;
      
      statusTitle.textContent = "Sayfa Optimize Ediliyor...";
      statusText.textContent = `Toplam ${total} kutu işleniyor.`;
      
      try {
        for (let q of pageQs) {
          statusText.textContent = `Soru ${q.soru_no} optimize ediliyor...`;
          const payload = {
            page_idx: q.page_idx,
            crop_left: q.crop_left,
            crop_top: q.crop_top,
            crop_right: q.crop_right,
            crop_bottom: q.crop_bottom,
            soru_no: q.soru_no
          };
          const res = await fetch(`/jobs/${jobId}/easy-editor/optimize-box`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (res.ok) {
            const data = await res.json();
            q.crop_left = data.crop_left;
            q.crop_top = data.crop_top;
            q.crop_right = data.crop_right;
            q.crop_bottom = data.crop_bottom;
            q.anchor_right = data.anchor_right;
            modifiedPages.add(pageIdx);
          }
        }
        
        for (let s of pageStems) {
          statusText.textContent = `Ortak Kök ${s.tempId} optimize ediliyor...`;
          const payload = {
            page_idx: s.page_idx,
            crop_left: s.crop_left,
            crop_top: s.crop_top,
            crop_right: s.crop_right,
            crop_bottom: s.crop_bottom,
            soru_no: 0
          };
          const res = await fetch(`/jobs/${jobId}/easy-editor/optimize-box`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
          });
          if (res.ok) {
            const data = await res.json();
            s.crop_left = data.crop_left;
            s.crop_top = data.crop_top;
            s.crop_right = data.crop_right;
            s.crop_bottom = data.crop_bottom;
            modifiedPages.add(pageIdx);
          }
        }
        
        reindexQuestions();
        statusTitle.textContent = prevTitle;
        statusText.textContent = "Optimizasyon başarıyla tamamlandı.";
        setTimeout(() => {
          statusText.textContent = prevText;
        }, 3000);
      } catch (err) {
        console.error("Toplu optimize hatası:", err);
        statusTitle.textContent = prevTitle;
        statusText.textContent = prevText;
        showAlert("Bazı kutular optimize edilirken hata oluştu.");
      }
    }

    function adjustSelectedBox(amount) {
      if (!selectedBoxId) return;
      adjustBoxSize(selectedBoxId, amount);
    }

    function adjustSelectedBoxSmart(action) {
      if (!selectedBoxId) return;
      adjustBoxBoundsSmart(selectedBoxId, action);
    }
    
    async function adjustBoxBoundsSmart(tempId, action) {
      let box = null;
      let isSubCrop = false;
      let isStem = false;
      
      if (tempId.startsWith('q_')) {
        box = questions.find(q => q.tempId === tempId);
      } else if (tempId.startsWith('s_')) {
        box = stems.find(s => s.tempId === tempId);
        isStem = true;
      } else if (tempId.startsWith('sub_')) {
        for (let q of questions) {
          if (q.sub_crops) {
            let found = q.sub_crops.find(sub => sub.tempId === tempId);
            if (found) {
              box = found;
              isSubCrop = true;
              break;
            }
          }
        }
      }
      
      if (!box) return;
      
      try {
        const payload = {
          page_idx: box.page_idx,
          crop_left: box.crop_left,
          crop_top: box.crop_top,
          crop_right: box.crop_right,
          crop_bottom: box.crop_bottom,
          soru_no: (!isStem && !isSubCrop && box.soru_no) ? box.soru_no : 0,
          action: action,
          hide_question_number: document.getElementById('toggle-hide-numbers').checked
        };
        
        const res = await fetch(`/jobs/${jobId}/easy-editor/adjust-box`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json'
          },
          body: JSON.stringify(payload)
        });
        
        if (!res.ok) throw new Error("Sınır ayarlama API hatası");
        
        const data = await res.json();
        modifiedPages.add(box.page_idx);
        
        box.crop_left = data.crop_left;
        box.crop_top = data.crop_top;
        box.crop_right = data.crop_right;
        box.crop_bottom = data.crop_bottom;
        if (!isStem && !isSubCrop) {
          box.anchor_right = data.anchor_right;
        }
        
        reindexQuestions();
        if (selectedBoxId === tempId) {
          showPropertiesPanel(tempId);
        }
      } catch (err) {
        console.error("Adjust box error:", err);
        showAlert("Kutu sınırları ayarlanamadı: " + err.message);
      }
    }
    
    function optimizeSelectedBox() {
      if (!selectedBoxId) return;
      if (selectedBoxId.startsWith('q_')) {
        optimizeBoxBounds(selectedBoxId);
      } else if (selectedBoxId.startsWith('s_')) {
        optimizeStemBounds(selectedBoxId);
      } else if (selectedBoxId.startsWith('sub_')) {
        let parentQ = null;
        for (let q of questions) {
          if (q.sub_crops) {
            let found = q.sub_crops.find(sub => sub.tempId === selectedBoxId);
            if (found) {
              parentQ = q;
              break;
            }
          }
        }
        if (parentQ) {
          optimizeSubCropBounds(parentQ.tempId, selectedBoxId);
        }
      }
    }
    
    function adjustBoxSize(tempId, amount) {
      let box = null;
      if (tempId.startsWith('q_')) {
        box = questions.find(q => q.tempId === tempId);
      } else if (tempId.startsWith('s_')) {
        box = stems.find(s => s.tempId === tempId);
      } else if (tempId.startsWith('sub_')) {
        for (let q of questions) {
          if (q.sub_crops) {
            let found = q.sub_crops.find(sub => sub.tempId === tempId);
            if (found) {
              box = found;
              break;
            }
          }
        }
      }
      
      if (box) {
        modifiedPages.add(box.page_idx);
        const page = pagesMap[box.page_idx];
        box.crop_left = parseFloat(Math.max(0, box.crop_left - amount).toFixed(2));
        box.crop_top = parseFloat(Math.max(0, box.crop_top - amount).toFixed(2));
        box.crop_right = parseFloat(Math.min(page.width, box.crop_right + amount).toFixed(2));
        box.crop_bottom = parseFloat(Math.min(page.height, box.crop_bottom + amount).toFixed(2));
        
        reindexQuestions();
      }
    }

    function updateCrosshairs(e, wrap) {
      let chH = document.getElementById('crosshair-h');
      let chV = document.getElementById('crosshair-v');
      
      if (!chH || !chV) {
        chH = document.createElement('div');
        chH.id = 'crosshair-h';
        chH.style.position = 'absolute';
        chH.style.left = '0';
        chH.style.right = '0';
        chH.style.height = '1px';
        chH.style.borderTop = '1px dashed #ef4444';
        chH.style.pointerEvents = 'none';
        chH.style.zIndex = '10000';
        
        chV = document.createElement('div');
        chV.id = 'crosshair-v';
        chV.style.position = 'absolute';
        chV.style.top = '0';
        chV.style.bottom = '0';
        chV.style.width = '1px';
        chV.style.borderLeft = '1px dashed #ef4444';
        chV.style.pointerEvents = 'none';
        chV.style.zIndex = '10000';
      }
      
      if (chH.parentNode !== wrap) wrap.appendChild(chH);
      if (chV.parentNode !== wrap) wrap.appendChild(chV);
      
      const rect = wrap.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      
      chH.style.top = y + 'px';
      chH.style.display = 'block';
      
      chV.style.left = x + 'px';
      chV.style.display = 'block';
    }
    
    function hideCrosshairs() {
      const chH = document.getElementById('crosshair-h');
      const chV = document.getElementById('crosshair-v');
      if (chH) chH.style.display = 'none';
      if (chV) chV.style.display = 'none';
    }

    let currentSplitPageIdx = null;

    function openSplitModal(pageIdx) {
      currentSplitPageIdx = pageIdx;
      const modal = document.getElementById('custom-test-split-modal');
      
      // Populate page selects
      const startSelect = document.getElementById('split-start-page');
      const endSelect = document.getElementById('split-end-page');
      startSelect.innerHTML = '';
      endSelect.innerHTML = '';
      
      pages.forEach(p => {
        const optStart = document.createElement('option');
        optStart.value = p.page_idx;
        optStart.textContent = `Sayfa ${p.page_idx + 1}`;
        optStart.selected = (p.page_idx === pageIdx);
        startSelect.appendChild(optStart);
        
        const optEnd = document.createElement('option');
        optEnd.value = p.page_idx;
        optEnd.textContent = `Sayfa ${p.page_idx + 1}`;
        optEnd.selected = (p.page_idx === pageIdx);
        endSelect.appendChild(optEnd);
      });
      
      document.querySelector('input[name="split-option"][value="from-here"]').checked = true;
      toggleSplitInputs();
      
      document.getElementById('split-modal-info-text').innerHTML = `<strong>Sayfa ${pageIdx + 1}</strong> üzerinde test ayırma/bölme işlemi gerçekleştirmek üzeresiniz.`;
      
      modal.style.display = 'flex';
      modal.offsetHeight; // Force reflow
      modal.classList.add('show');
    }

    function hideSplitModal() {
      const modal = document.getElementById('custom-test-split-modal');
      modal.classList.remove('show');
      setTimeout(() => {
        modal.style.display = 'none';
      }, 200);
    }
    
    function toggleSplitInputs() {
      const opt = document.querySelector('input[name="split-option"]:checked').value;
      const rangeContainer = document.getElementById('split-range-container');
      if (opt === 'range') {
        rangeContainer.style.display = 'flex';
      } else {
        rangeContainer.style.display = 'none';
      }
    }
    
    function executeSplitTest() {
      const opt = document.querySelector('input[name="split-option"]:checked').value;
      let startIdx, endIdx;
      
      if (opt === 'from-here') {
        startIdx = currentSplitPageIdx;
        endIdx = pages.length - 1;
      } else {
        startIdx = parseInt(document.getElementById('split-start-page').value);
        endIdx = parseInt(document.getElementById('split-end-page').value);
        if (startIdx > endIdx) {
          showAlert("Başlangıç sayfası bitiş sayfasından büyük olamaz!");
          return;
        }
      }
      
      splitTestBulk(startIdx, endIdx);
      hideSplitModal();
    }

    function splitTestBulk(startPageIdx, endPageIdx) {
      saveState();

      const nextNo = tests.length > 0 ? Math.max(...tests.map(t => t.test_no)) + 1 : 1;
      const newTestName = `Test-${nextNo}`;
      
      tests.push({ test_no: nextNo, test_name: newTestName });
      
      const pagesToMark = new Set();
      
      questions.forEach(q => {
        if (q.page_idx >= startPageIdx && q.page_idx <= endPageIdx) {
          q.test_no = nextNo;
          q.test_name = newTestName;
          pagesToMark.add(q.page_idx);
        }
      });
      
      stems.forEach(s => {
        if (s.page_idx >= startPageIdx && s.page_idx <= endPageIdx) {
          s.test_no = nextNo;
          pagesToMark.add(s.page_idx);
        }
      });
      
      stems.forEach(s => {
        s.targetTempIds = s.targetTempIds.filter(tid => {
          const q = questions.find(item => item.tempId === tid);
          if (!q) return false;
          if (q.test_no !== s.test_no) {
            pagesToMark.add(s.page_idx);
            pagesToMark.add(q.page_idx);
            return false;
          }
          return true;
        });
      });
      
      pagesToMark.forEach(p => modifiedPages.add(p));
      
      activeTestNo = nextNo;
      reindexQuestions();
      scrollToFirstPageOfTest(activeTestNo);
    }
  </script>

  <!-- Custom Test Management Modal -->
  <div id="custom-test-split-modal" class="custom-modal">
    <div class="custom-modal-content" style="max-width: 400px; text-align: left;">
      <h2 style="margin-top: 0; color: var(--accent); margin-bottom: 15px; display: flex; align-items: center; gap: 8px;">
        <span>📂</span> Test Oluştur / Böl
      </h2>
      <div style="margin-bottom: 20px;">
        <p style="font-size: 0.9rem; color: var(--text-muted); margin-bottom: 15px;" id="split-modal-info-text">
          Bu sayfa itibariyle soruları yeni bir teste taşıyabilir veya bir sayfa aralığı seçerek yeni test oluşturabilirsiniz.
        </p>
        
        <div style="display: flex; flex-direction: column; gap: 12px;">
          <label style="display: flex; align-items: flex-start; gap: 8px; cursor: pointer;">
            <input type="radio" name="split-option" value="from-here" checked style="margin-top: 3px;" onchange="toggleSplitInputs()">
            <div>
              <strong style="font-size: 0.9rem;">Bu sayfadan itibaren yeni test yap</strong>
              <div style="font-size: 0.8rem; color: var(--text-muted);">Seçili sayfa ve sonrasındaki tüm sayfalar yeni teste taşınır.</div>
            </div>
          </label>
          
          <label style="display: flex; align-items: flex-start; gap: 8px; cursor: pointer;">
            <input type="radio" name="split-option" value="range" style="margin-top: 3px;" onchange="toggleSplitInputs()">
            <div>
              <strong style="font-size: 0.9rem;">Belirli sayfa aralığını yeni test yap</strong>
              <div style="font-size: 0.8rem; color: var(--text-muted);">Yalnızca seçtiğiniz sayfa aralığındaki sorular yeni teste taşınır.</div>
            </div>
          </label>
        </div>
        
        <div id="split-range-container" style="display: none; margin-top: 15px; padding-left: 24px; gap: 10px; align-items: center;">
          <div style="display: flex; flex-direction: column; gap: 4px; flex: 1;">
            <label style="font-size: 0.75rem; color: var(--text-muted); font-weight: bold;">Başlangıç</label>
            <select id="split-start-page" class="test-select" style="margin-bottom: 0;">
            </select>
          </div>
          <div style="display: flex; flex-direction: column; gap: 4px; flex: 1;">
            <label style="font-size: 0.75rem; color: var(--text-muted); font-weight: bold;">Bitiş</label>
            <select id="split-end-page" class="test-select" style="margin-bottom: 0;">
            </select>
          </div>
        </div>
      </div>
      <div class="custom-modal-buttons" style="justify-content: flex-end; gap: 10px;">
        <button class="modal-btn modal-btn-cancel" onclick="hideSplitModal()">Vazgeç</button>
        <button class="modal-btn modal-btn-ok" onclick="executeSplitTest()">Uygula</button>
      </div>
    </div>
  </div>

  <!-- Custom Confirmation Modal -->
  <div id="custom-confirm-modal" class="custom-modal">
    <div class="custom-modal-content">
      <div class="custom-modal-icon">⚠️</div>
      <div class="custom-modal-text" id="custom-confirm-text">Bu sayfadaki tüm mevcut kutular silinecek ve orijinal otomatik tespit edilen kutular yüklenecek. Emin misiniz?</div>
      <div class="custom-modal-buttons">
        <button class="modal-btn modal-btn-cancel" id="custom-confirm-cancel">Vazgeç</button>
        <button class="modal-btn modal-btn-ok" id="custom-confirm-ok">Evet, Devam Et</button>
      </div>
    </div>
  </div>

  <!-- Custom Alert Modal -->
  <div id="custom-alert-modal" class="custom-modal">
    <div class="custom-modal-content">
      <div class="custom-modal-icon" style="color: #ef4444;">⚠️</div>
      <div class="custom-modal-text" id="custom-alert-text">Hata mesajı</div>
      <div class="custom-modal-buttons">
        <button class="modal-btn modal-btn-ok" id="custom-alert-ok">Tamam</button>
      </div>
    </div>
  </div>

  <!-- Custom Help Modal -->
  <div id="custom-help-modal" class="custom-modal">
    <div class="custom-modal-content" style="max-width: 600px; text-align: left;">
      <h2 style="margin-top: 0; color: var(--accent); display: flex; align-items: center; gap: 8px;">
        <span>❓</span> Nasıl Kullanılır?
      </h2>
      <div style="max-height: 400px; overflow-y: auto; padding-right: 8px; margin: 15px 0;">
        <h4 style="margin: 10px 0 5px 0; color: var(--text);">Klavye Kısayolları</h4>
        <table style="width: 100%; border-collapse: collapse; margin-bottom: 15px;">
          <tr style="border-bottom: 1px solid var(--border);">
            <td style="padding: 6px 0; font-weight: bold; width: 30%;">S</td>
            <td style="padding: 6px 0; color: var(--text-muted);">Kutu Seç & Düzenle Modu (Seçilen kutuyu sürükleyebilir veya kenarlarından boyutlandırabilirsiniz)</td>
          </tr>
          <tr style="border-bottom: 1px solid var(--border);">
            <td style="padding: 6px 0; font-weight: bold;">Q</td>
            <td style="padding: 6px 0; color: var(--text-muted);">Yeni Soru Çiz Modu (Farenin sol tuşuna basılı tutarak sürükleyip yeni soru kutusu çizebilirsiniz)</td>
          </tr>
          <tr style="border-bottom: 1px solid var(--border);">
            <td style="padding: 6px 0; font-weight: bold;">W</td>
            <td style="padding: 6px 0; color: var(--text-muted);">Yeni Ortak Kök Çiz Modu (Birden fazla soruyu ilgilendiren ortak metinleri çizmek için kullanılır)</td>
          </tr>
          <tr style="border-bottom: 1px solid var(--border);">
            <td style="padding: 6px 0; font-weight: bold;">E</td>
            <td style="padding: 6px 0; color: var(--text-muted);">İşaret Noktası Ekle Modu (Sayfada soruların başlangıç yerlerine 📍 tıklayarak işaretçi yerleştirebilirsiniz)</td>
          </tr>
          <tr style="border-bottom: 1px solid var(--border);">
            <td style="padding: 6px 0; font-weight: bold;">Ctrl + Z</td>
            <td style="padding: 6px 0; color: var(--text-muted);">Son işlemi Geri Alır</td>
          </tr>
          <tr style="border-bottom: 1px solid var(--border);">
            <td style="padding: 6px 0; font-weight: bold;">Ctrl + Y</td>
            <td style="padding: 6px 0; color: var(--text-muted);">Geri alınan işlemi İleri Alır (Yinele)</td>
          </tr>
        </table>

        <h4 style="margin: 10px 0 5px 0; color: var(--text);">Akıllı Fonksiyonlar</h4>
        <ul style="margin: 0; padding-left: 20px; color: var(--text-muted); line-height: 1.5;">
          <li style="margin-bottom: 8px;"><strong>Otomatik Algıla / İşaretlerden Algıla</strong>: Sayfada hiç işaretçi (📍) yoksa sistem otomatik olarak soruları bulmaya çalışır. Eğer 📍 işaretçi eklediyseniz buton kırmızı <strong>İşaretlerden Algıla</strong> moduna döner. Sistem soruları bu işaretlerin olduğu yerlerden çakışma ve üst üste binme olmadan böler.</li>
          <li style="margin-bottom: 8px;"><strong>Yeniden Optimize Et (Soru üzerindeki 🪄 butonu)</strong>: Çizdiğiniz veya algılanan kutunun sınırlarını, içindeki metin bloklarına tam oturacak şekilde akıllıca daraltır veya genişletir.</li>
          <li style="margin-bottom: 8px;"><strong>Hassas Boyutlandırma (Soru üzerindeki + ve - butonları)</strong>: Seçili kutunun sınırlarını dikeyde yapay zeka destekli metin algılama usulüyle büyütür (+) veya küçültür (-).</li>
        </ul>
      </div>
      <div class="custom-modal-buttons" style="justify-content: flex-end;">
        <button class="modal-btn modal-btn-ok" onclick="hideHelpModal()">Kapat</button>
      </div>
    </div>
  </div>

</body>
</html>
"""
    font_links = "" if web_app.LOCAL_MODE else """
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&family=Inter:wght@300;400;500;700&display=swap" rel="stylesheet">"""
    html_res = html.replace("FONT_LINKS_PLACEHOLDER", font_links)
    html_res = html_res.replace("JSON_DATA_PLACEHOLDER", json.dumps(data, ensure_ascii=False))
    status = data.get("status", "queued")
    if status in ("error", "empty", "cancelled"):
        overlay_display = "flex"
    elif status in ("queued", "rendering", "processing"):
        overlay_display = "flex"
    else:
        overlay_display = "none"

    return html_res

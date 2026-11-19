
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CAMERA ALPR - CONTOUR + PERSPECTIVE WARP + CONNECTED COMPONENTS
+ UKNUMBER TEMPLATE MATCHING

Module tich hop cho main.py cua he thong bai do xe 4 o.
Khong dung Tesseract, YOLO, GPIO, RFID hay servo trong file nay.

API tuong thich main.py:
    PlateRecognitionSystem()
    open_preview_windows()
    update_presence(gate, present, now)
    collect_results()
    show_windows()
    get_plate(gate)
    get_plate_age(gate, now=None)
    status(gate)
    clear_plate(gate)
    close()

Bien so dang nhan dien: 2 so + 1 chu + 5 so (8 ky tu).
"""

import os
import time
import queue
import threading
from collections import Counter, deque

import cv2
import numpy as np

from config import *
from storage import normalize_plate, valid_plate, format_plate

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except Exception:
    Image = ImageDraw = ImageFont = None
    PIL_AVAILABLE = False

try:
    cv2.setNumThreads(1)
except Exception:
    pass


# ============================================================
# 1. CAC GIA TRI CO THE KHONG CO TRONG config.py CU
# ============================================================

IR_HOLD_SECONDS = float(globals().get("PLATE_IR_HOLD_SECONDS", 2.5))
TEMPLATE_MIN_CONFIDENCE = float(globals().get("TEMPLATE_MIN_CONFIDENCE", 0.48))
TEMPLATE_MIN_CHAR_CONFIDENCE = float(globals().get("TEMPLATE_MIN_CHAR_CONFIDENCE", 0.30))
TEMPLATE_FONT_PATH = globals().get("UKNUMBER_FONT_PATH", "")
TEMPLATE_CANVAS_W = 32
TEMPLATE_CANVAS_H = 48
TEMPLATE_GLYPH_H = 40
TEMPLATE_MAX_GLYPH_W = 28


# ============================================================
# 2. BO MAU KY TU UKNUMBER
# ============================================================

class UkNumberTemplateBank:
    """Tao 10 mau so va 26 mau chu. Uu tien font UKNumberPlate.ttf."""

    def __init__(self, font_path=None):
        self.digits = {}
        self.letters = {}
        self.font_source = "FALLBACK_OPENCV"

        resolved = self._find_font_file(font_path or TEMPLATE_FONT_PATH)
        if PIL_AVAILABLE and resolved:
            try:
                self._build_from_ttf(resolved)
                self.font_source = resolved
            except Exception as exc:
                print("[TEMPLATE WARNING] Khong nap duoc font: {}".format(exc), flush=True)
                self._build_fallback_opencv()
        else:
            self._build_fallback_opencv()
            if not PIL_AVAILABLE:
                reason = "Pillow chua duoc cai dat"
            else:
                reason = "Khong tim thay UKNumberPlate.ttf"
            print("[TEMPLATE WARNING] {} - dung font OpenCV du phong.".format(reason), flush=True)

        print(
            "[TEMPLATE] Da nap {} so + {} chu | FONT={}".format(
                len(self.digits), len(self.letters), self.font_source
            ),
            flush=True
        )

    @staticmethod
    def _find_font_file(custom_path):
        if custom_path and os.path.isfile(custom_path):
            return custom_path

        bases = [
            os.path.dirname(os.path.abspath(__file__)),
            os.getcwd(),
            os.path.expanduser("~/Downloads/parking_system"),
            os.path.expanduser("~/.fonts"),
            "/usr/local/share/fonts",
            "/usr/share/fonts/truetype",
        ]

        visited = set()
        for base in bases:
            if not os.path.isdir(base) or base in visited:
                continue
            visited.add(base)
            for root, _, files in os.walk(base):
                for name in files:
                    low = name.lower()
                    if "uknumber" in low and low.endswith((".ttf", ".otf")):
                        return os.path.join(root, name)
        return None

    @classmethod
    def normalize_glyph(cls, glyph):
        if glyph is None or glyph.size == 0:
            return np.zeros((TEMPLATE_CANVAS_H, TEMPLATE_CANVAS_W), dtype=np.uint8)

        binary = (glyph > 0).astype(np.uint8) * 255
        points = cv2.findNonZero(binary)
        if points is None:
            return np.zeros((TEMPLATE_CANVAS_H, TEMPLATE_CANVAS_W), dtype=np.uint8)

        x, y, w, h = cv2.boundingRect(points)
        if w <= 0 or h <= 0:
            return np.zeros((TEMPLATE_CANVAS_H, TEMPLATE_CANVAS_W), dtype=np.uint8)

        cropped = binary[y:y + h, x:x + w]
        scale = TEMPLATE_GLYPH_H / float(h)
        new_h = TEMPLATE_GLYPH_H
        new_w = max(1, int(round(w * scale)))

        if new_w > TEMPLATE_MAX_GLYPH_W:
            scale = TEMPLATE_MAX_GLYPH_W / float(w)
            new_w = TEMPLATE_MAX_GLYPH_W
            new_h = max(1, int(round(h * scale)))

        resized = cv2.resize(
            cropped, (new_w, new_h),
            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
        )
        _, resized = cv2.threshold(resized, 110, 255, cv2.THRESH_BINARY)

        canvas = np.zeros((TEMPLATE_CANVAS_H, TEMPLATE_CANVAS_W), dtype=np.uint8)
        y_off = (TEMPLATE_CANVAS_H - new_h) // 2
        x_off = (TEMPLATE_CANVAS_W - new_w) // 2
        canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
        return canvas

    @staticmethod
    def _make_variants(canvas):
        # Bien the nhe do day net de giam lech giua mau va anh chup.
        k = np.ones((2, 2), np.uint8)
        return (
            canvas,
            cv2.erode(canvas, k, iterations=1),
            cv2.dilate(canvas, k, iterations=1),
        )

    def _build_from_ttf(self, path):
        font = ImageFont.truetype(path, 120)
        for ch in "0123456789":
            self.digits[ch] = self._render_pil(font, ch)
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            self.letters[ch] = self._render_pil(font, ch)

    def _render_pil(self, font, ch):
        img = Image.new("L", (180, 180), 0)
        draw = ImageDraw.Draw(img)
        try:
            bbox = draw.textbbox((0, 0), ch, font=font, stroke_width=0)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            x = (180 - tw) // 2 - bbox[0]
            y = (180 - th) // 2 - bbox[1]
        except Exception:
            x, y = 20, 15
        draw.text((x, y), ch, fill=255, font=font)
        arr = np.array(img, dtype=np.uint8)
        _, binary = cv2.threshold(arr, 127, 255, cv2.THRESH_BINARY)
        return self._make_variants(self.normalize_glyph(binary))

    def _build_fallback_opencv(self):
        for ch in "0123456789":
            self.digits[ch] = self._render_cv(ch)
        for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            self.letters[ch] = self._render_cv(ch)

    def _render_cv(self, ch):
        img = np.zeros((150, 120), dtype=np.uint8)
        cv2.putText(img, ch, (12, 118), cv2.FONT_HERSHEY_DUPLEX, 3.2, 255, 7, cv2.LINE_AA)
        return self._make_variants(self.normalize_glyph(img))

    def match_char(self, glyph_mask, expect_digit=True):
        norm = self.normalize_glyph(glyph_mask)
        pool = self.digits if expect_digit else self.letters
        best_char = ""
        best_score = -1.0
        second_score = -1.0

        for char, variants in pool.items():
            for template in variants:
                score = float(cv2.matchTemplate(norm, template, cv2.TM_CCOEFF_NORMED)[0, 0])
                if score > best_score:
                    second_score = best_score
                    best_score = score
                    best_char = char
                elif score > second_score:
                    second_score = score

        margin = best_score - max(-1.0, second_score)
        return best_char, best_score, margin, norm


# ============================================================
# 3. ROI, CONTOUR VA PERSPECTIVE WARP
# ============================================================

_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))


def clamp_roi(roi, frame_width, frame_height):
    x1, y1, x2, y2 = roi
    sx = frame_width / float(ROI_REFERENCE_WIDTH)
    sy = frame_height / float(ROI_REFERENCE_HEIGHT)
    x1 = max(0, min(int(round(x1 * sx)), frame_width - 1))
    x2 = max(1, min(int(round(x2 * sx)), frame_width))
    y1 = max(0, min(int(round(y1 * sy)), frame_height - 1))
    y2 = max(1, min(int(round(y2 * sy)), frame_height))
    return x1, y1, x2, y2


def crop_plate_roi(frame, roi):
    if frame is None or frame.size == 0:
        return None
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = clamp_roi(roi, w, h)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = frame[y1:y2, x1:x2]
    return crop if crop.size else None


def order_quad_points(points):
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).reshape(-1)
    ordered = np.zeros((4, 2), dtype=np.float32)
    ordered[0] = pts[np.argmin(s)]  # TL
    ordered[2] = pts[np.argmax(s)]  # BR
    ordered[1] = pts[np.argmin(d)]  # TR
    ordered[3] = pts[np.argmax(d)]  # BL
    return ordered


def _warp_quad(image, quad):
    ordered = order_quad_points(quad)
    tl, tr, br, bl = ordered
    width = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    height = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
    if width < 30 or height < 18:
        return None
    aspect = width / float(height)
    if not 1.15 <= aspect <= 4.8:
        return None
    dst = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32
    )
    matrix = cv2.getPerspectiveTransform(ordered, dst)
    warped = cv2.warpPerspective(
        image, matrix, (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE
    )
    return warped


def find_warped_candidates(roi_bgr):
    """Tim mot so ung vien bien so va them ROI phong to lam du phong."""
    if roi_bgr is None or roi_bgr.size == 0:
        return []

    h, w = roi_bgr.shape[:2]
    roi_area = float(h * w)
    gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    masks = []
    _, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    masks.append(otsu)
    _, bright = cv2.threshold(gray, 185, 255, cv2.THRESH_BINARY)
    masks.append(bright)

    candidates = []
    seen = []
    kernels = [
        cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
        cv2.getStructuringElement(cv2.MORPH_RECT, (9, 5)),
    ]

    for mask in masks:
        for kernel in kernels:
            blob = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=1)
            contours, _ = cv2.findContours(blob, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours = sorted(contours, key=cv2.contourArea, reverse=True)[:10]

            for contour in contours:
                area = cv2.contourArea(contour)
                if not roi_area * 0.035 <= area <= roi_area * 0.96:
                    continue

                x, y, bw, bh = cv2.boundingRect(contour)
                if any(abs(x - sx) < 8 and abs(y - sy) < 8 and abs(bw - sw) < 12 and abs(bh-sh) < 12
                       for sx, sy, sw, sh in seen):
                    continue
                seen.append((x, y, bw, bh))

                rect = cv2.minAreaRect(contour)
                rw, rh = rect[1]
                if rw <= 1 or rh <= 1:
                    continue
                aspect = max(rw, rh) / min(rw, rh)
                if not 1.15 <= aspect <= 4.8:
                    continue

                peri = cv2.arcLength(contour, True)
                approx = cv2.approxPolyDP(contour, 0.025 * peri, True)
                if len(approx) == 4 and cv2.isContourConvex(approx):
                    quad = approx.reshape(4, 2)
                else:
                    quad = cv2.boxPoints(rect)

                warped = _warp_quad(roi_bgr, quad)
                if warped is not None:
                    candidates.append((warped, "WARP_{}x{}".format(warped.shape[1], warped.shape[0])))

    # ROI 2x la du phong bat buoc, dac biet khi nen bien va nen mo hinh gan mau nhau.
    roi_up = cv2.resize(roi_bgr, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LANCZOS4)
    candidates.append((roi_up, "ROI_2X"))

    # Gioi han so ung vien de khong lam qua tai Pi.
    return candidates[:8]


# ============================================================
# 4. PHAN DOAN 8 KY TU (3 TREN + 5 DUOI)
# ============================================================


def _cluster_rows(blobs):
    if not blobs:
        return []
    rows = []
    for blob in sorted(blobs, key=lambda b: b["cy"]):
        added = False
        for row in rows:
            ref_y = float(np.mean([b["cy"] for b in row]))
            ref_h = float(np.median([b["h"] for b in row]))
            if abs(blob["cy"] - ref_y) <= max(blob["h"], ref_h) * 0.62:
                row.append(blob)
                added = True
                break
        if not added:
            rows.append([blob])

    out = []
    for row in rows:
        row.sort(key=lambda b: b["x"])
        if len(row) < 3:
            continue
        median_h = float(np.median([b["h"] for b in row]))
        clusters = [[row[0]]]
        for prev, cur in zip(row, row[1:]):
            gap = cur["x"] - (prev["x"] + prev["w"])
            if gap <= median_h * 1.45:
                clusters[-1].append(cur)
            else:
                clusters.append([cur])
        out.extend(cl for cl in clusters if len(cl) >= 3)
    out.sort(key=lambda r: float(np.mean([b["cy"] for b in r])))
    return out


def _best_consecutive_group(row, expected, target_center=None):
    if len(row) < expected:
        return None
    best = None
    best_score = -1e9
    for start in range(len(row) - expected + 1):
        group = row[start:start + expected]
        heights = np.array([b["h"] for b in group], dtype=np.float32)
        widths = np.array([b["w"] for b in group], dtype=np.float32)
        centers = np.array([b["x"] + b["w"] / 2.0 for b in group], dtype=np.float32)
        bottoms = np.array([b["y"] + b["h"] for b in group], dtype=np.float32)
        h_cv = float(np.std(heights) / (np.mean(heights) + 1e-6))
        w_cv = float(np.std(widths) / (np.mean(widths) + 1e-6))
        bottom_error = float(np.std(bottoms)) / (np.mean(heights) + 1e-6)
        gaps = np.array([
            group[i + 1]["x"] - (group[i]["x"] + group[i]["w"])
            for i in range(expected - 1)
        ], dtype=np.float32)
        gap_cv = float(np.std(gaps) / (abs(np.mean(gaps)) + 1.0))
        center = float((centers[0] + centers[-1]) / 2.0)
        center_penalty = 0.0 if target_center is None else abs(center - target_center) / max(1.0, np.mean(heights))
        score = 2.0 - 3.0 * h_cv - 1.2 * w_cv - 0.8 * bottom_error - 0.35 * gap_cv - 0.25 * center_penalty
        if score > best_score:
            best_score = score
            best = group
    return best


def extract_8_chars_from_image(image_bgr, use_adaptive=False):
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = _CLAHE.apply(gray)
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=1.0)
    sharp = cv2.addWeighted(gray, 1.5, blur, -0.5, 0)

    if use_adaptive:
        foreground = cv2.adaptiveThreshold(
            sharp, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 31, 11
        )
    else:
        _, foreground = cv2.threshold(
            sharp, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )

    h, w = foreground.shape[:2]
    # Lo sat 1px, tranh vien ROI sat mep; khong cat noi dung giua anh.
    foreground[:1, :] = 0
    foreground[-1:, :] = 0
    foreground[:, :1] = 0
    foreground[:, -1:] = 0

    # Dong nhe cac net bi dut, kernel nho de han che dinh hai ky tu ke nhau.
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    connected = cv2.morphologyEx(foreground, cv2.MORPH_CLOSE, close_kernel, iterations=1)

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(connected, connectivity=8)
    min_h = max(7, int(h * 0.075))
    max_h = int(h * 0.92)
    min_w = 2
    max_w = max(4, int(w * 0.28))
    all_blobs = []

    for idx in range(1, n):
        x = int(stats[idx, cv2.CC_STAT_LEFT])
        y = int(stats[idx, cv2.CC_STAT_TOP])
        bw = int(stats[idx, cv2.CC_STAT_WIDTH])
        bh = int(stats[idx, cv2.CC_STAT_HEIGHT])
        area = int(stats[idx, cv2.CC_STAT_AREA])
        cx, cy = centroids[idx]

        if not (min_h <= bh <= max_h and min_w <= bw <= max_w):
            continue
        if area < max(8, int(h * w * 0.00010)):
            continue
        aspect = bw / float(max(1, bh))
        fill = area / float(max(1, bw * bh))
        if not (0.10 <= aspect <= 1.25 and 0.10 <= fill <= 0.96):
            continue

        # Luu mask theo bounding box, giu ca cac net trong thanh phan da lien thong.
        mask = np.zeros((bh, bw), dtype=np.uint8)
        roi_labels = labels[y:y + bh, x:x + bw]
        mask[roi_labels == idx] = 255
        all_blobs.append({
            "x": x, "y": y, "w": bw, "h": bh,
            "cx": float(cx), "cy": float(cy),
            "mask": mask, "area": area
        })

    rows = _cluster_rows(all_blobs)
    selected = []

    # Tim cap hang 3 + 5, co do chong lap theo truc X.
    pair_options = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            top_row, bottom_row = rows[i], rows[j]
            top_group = _best_consecutive_group(top_row, 3)
            bottom_group = _best_consecutive_group(bottom_row, 5)
            if top_group is None or bottom_group is None:
                continue

            tx1 = top_group[0]["x"]
            tx2 = top_group[-1]["x"] + top_group[-1]["w"]
            bx1 = bottom_group[0]["x"]
            bx2 = bottom_group[-1]["x"] + bottom_group[-1]["w"]
            overlap = min(tx2, bx2) - max(tx1, bx1)
            if overlap <= 0:
                continue

            ty = float(np.mean([b["cy"] for b in top_group]))
            by = float(np.mean([b["cy"] for b in bottom_group]))
            avg_h = float(np.median([b["h"] for b in top_group + bottom_group]))
            vertical = by - ty
            if not (0.35 * avg_h <= vertical <= 3.0 * avg_h):
                continue

            score = overlap / max(1, min(tx2 - tx1, bx2 - bx1)) - abs(vertical / avg_h - 1.35) * 0.05
            pair_options.append((score, top_group, bottom_group))

    if pair_options:
        _, top_group, bottom_group = max(pair_options, key=lambda item: item[0])
        selected = top_group + bottom_group

    # Du phong: mot hang 8 ky tu.
    if not selected:
        for row in rows:
            group = _best_consecutive_group(row, 8)
            if group is not None:
                selected = group
                break

    debug = cv2.cvtColor(foreground, cv2.COLOR_GRAY2BGR)
    for blob in all_blobs:
        x, y, bw, bh = blob["x"], blob["y"], blob["w"], blob["h"]
        cv2.rectangle(debug, (x, y), (x + bw, y + bh), (0, 0, 180), 1)
    for idx, blob in enumerate(selected):
        x, y, bw, bh = blob["x"], blob["y"], blob["w"], blob["h"]
        cv2.rectangle(debug, (x, y), (x + bw, y + bh), (0, 255, 0), 2)
        cv2.putText(debug, str(idx + 1), (x, max(14, y - 3)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)

    char_masks = []
    for blob in selected:
        # Lay lai mask tu anh foreground goc trong bbox (khong chi dung component label).
        x, y, bw, bh = blob["x"], blob["y"], blob["w"], blob["h"]
        char_masks.append(foreground[y:y + bh, x:x + bw].copy())

    return char_masks, debug, len(all_blobs)


# ============================================================
# 5. DEBUG STRIP + NHAN DIEN MOT UNG VIEN
# ============================================================


def build_debug_strip(debug_image, normalized_glyphs, scores):
    strip_h = 68
    thumb = cv2.resize(debug_image, (112, strip_h), interpolation=cv2.INTER_AREA)
    cv2.rectangle(thumb, (0, 0), (111, strip_h - 1), (0, 255, 255), 1)
    panels = [thumb]
    for idx in range(8):
        panel = np.zeros((strip_h, 24, 3), dtype=np.uint8)
        if idx < len(normalized_glyphs):
            glyph = cv2.resize(normalized_glyphs[idx], (20, 44), interpolation=cv2.INTER_NEAREST)
            panel[12:56, 2:22] = cv2.cvtColor(glyph, cv2.COLOR_GRAY2BGR)
            score = scores[idx] if idx < len(scores) else 0.0
            color = (0, 220, 0) if score >= TEMPLATE_MIN_CONFIDENCE else (0, 0, 255)
            cv2.rectangle(panel, (0, 0), (23, strip_h - 1), color, 1)
            cv2.putText(panel, str(idx + 1), (4, 9), cv2.FONT_HERSHEY_SIMPLEX, 0.25, color, 1)
        else:
            cv2.rectangle(panel, (0, 0), (23, strip_h - 1), (70, 70, 70), 1)
        panels.append(panel)
    return np.hstack(panels)


def recognize_plate_debug(frame, roi, template_bank):
    crop = crop_plate_roi(frame, roi)
    if crop is None:
        return "", {"raw": "ROI_ERR", "method": "ROI_ERROR", "confidence": 0.0, "details": []}, None

    candidates = find_warped_candidates(crop)
    best_valid = None
    best_debug = None

    for image_candidate, source_tag in candidates:
        for adaptive in (False, True):
            masks, debug, blob_count = extract_8_chars_from_image(image_candidate, adaptive)
            mode = "{}_{}".format(source_tag, "ADAPTIVE" if adaptive else "OTSU")

            # Luu ung vien phan doan tot nhat de hien thi khi chua nhan dien duoc.
            if (best_debug is None or len(masks) > best_debug[0] or
                    (len(masks) == best_debug[0] and blob_count > best_debug[1])):
                best_debug = (len(masks), blob_count, mode, debug)

            if len(masks) != 8:
                continue

            chars, scores, margins, normalized, details = [], [], [], [], []
            for pos, mask in enumerate(masks):
                expect_digit = (pos != 2)
                char, score, margin, norm = template_bank.match_char(mask, expect_digit)
                chars.append(char)
                scores.append(score)
                margins.append(margin)
                normalized.append(norm)
                details.append((pos, char, score, margin))

            plate = normalize_plate("".join(chars))
            avg_score = float(np.mean(scores))
            min_score = float(np.min(scores))
            min_margin = float(np.min(margins))
            strip = build_debug_strip(debug, normalized, scores)

            meta = {
                "raw": plate,
                "method": mode,
                "confidence": avg_score,
                "min_confidence": min_score,
                "min_margin": min_margin,
                "details": details,
                "blob_count": blob_count,
            }

            if (valid_plate(plate)
                    and avg_score >= TEMPLATE_MIN_CONFIDENCE
                    and min_score >= TEMPLATE_MIN_CHAR_CONFIDENCE):
                if best_valid is None or avg_score > best_valid[1]["confidence"]:
                    best_valid = (plate, meta, strip)

            elif best_debug is not None:
                # Neu phan doan du 8 ky tu nhung diem thap, giu debug de xem.
                if best_debug[0] < 8:
                    best_debug = (8, blob_count, mode, debug)

    if best_valid is not None:
        return best_valid

    if best_debug is None:
        return "", {"raw": "NO_CANDIDATE", "method": "NO_CANDIDATE", "confidence": 0.0, "details": []}, None

    count, blob_count, mode, debug = best_debug
    strip = build_debug_strip(debug, [], [])
    return "", {
        "raw": "BLOBS={}_SELECTED={}".format(blob_count, count),
        "method": mode + "_SEGMENT_OR_SCORE_FAIL",
        "confidence": 0.0,
        "details": [],
    }, strip


# ============================================================
# 6. BO PHIEU THEO VI TRI KY TU
# ============================================================


def vote_by_character_position(votes, min_matches=2):
    if len(votes) < min_matches:
        return "", 0.0
    total = len(votes)
    chars = []
    agreements = []
    for pos in range(8):
        counts = Counter(p[pos] for p in votes if len(p) == 8)
        if not counts:
            return "", 0.0
        char, count = counts.most_common(1)[0]
        if count < min_matches:
            return "", 0.0
        chars.append(char)
        agreements.append(count / float(total))
    result = "".join(chars)
    if not valid_plate(result):
        return "", 0.0
    return result, float(sum(agreements) / len(agreements))


def frame_quality(frame, roi):
    crop = crop_plate_roi(frame, roi)
    if crop is None:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    sharpness = float(cv2.Laplacian(small, cv2.CV_64F).var())
    brightness = float(np.mean(small))
    if brightness < 25 or brightness > 240:
        sharpness *= 0.5
    return sharpness


# ============================================================
# 7. CAMERA STREAM
# ============================================================

class CameraStream:
    def __init__(self, device, label):
        self.device = device
        self.label = label
        self.active_mode = False
        self.cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            self.cap.release()
            raise RuntimeError("Khong mo duoc camera {} ({})".format(label, device))

        fourcc = cv2.VideoWriter_fourcc(*CAMERA_FOURCC)
        self.cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, CAMERA_FPS)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = float(self.cap.get(cv2.CAP_PROP_FPS))
        actual_fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        actual_format = "".join(chr((actual_fourcc >> (8 * i)) & 0xFF) for i in range(4))
        print("[CAMERA] {} | {} | {}x{} | FPS={:.1f} | FORMAT={}".format(
            label, device, actual_w, actual_h, actual_fps, actual_format
        ), flush=True)

        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.frame = None
        self.frame_time = 0.0
        self.thread = threading.Thread(target=self._reader, name="CAM-" + label, daemon=True)
        self.thread.start()

    def set_active_mode(self, active):
        self.active_mode = bool(active)

    def _reader(self):
        last_decode = 0.0
        while not self.stop_event.is_set():
            if not self.cap.grab():
                time.sleep(0.02)
                continue
            now = time.monotonic()
            min_interval = 0.02 if self.active_mode else 0.08
            if now - last_decode < min_interval:
                time.sleep(0.002)
                continue
            ok, frame = self.cap.retrieve()
            if ok and frame is not None:
                last_decode = now
                with self.lock:
                    self.frame = frame
                    self.frame_time = now

    def get_frame(self):
        with self.lock:
            if self.frame is None:
                return None, 0.0
            return self.frame.copy(), self.frame_time

    def close(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
        self.cap.release()


# ============================================================
# 8. PLATE RECOGNITION SYSTEM - API CHO main.py
# ============================================================

class PlateRecognitionSystem:
    def __init__(self):
        self.config = {
            "VAO": {
                "device": ENTRY_CAMERA_DEVICE,
                "roi": ENTRY_PLATE_ROI,
                "window_x": ENTRY_WINDOW_X,
                "window_y": ENTRY_WINDOW_Y,
            },
            "RA": {
                "device": EXIT_CAMERA_DEVICE,
                "roi": EXIT_PLATE_ROI,
                "window_x": EXIT_WINDOW_X,
                "window_y": EXIT_WINDOW_Y,
            },
        }
        self.window_names = {"VAO": "CAMERA CONG VAO", "RA": "CAMERA CONG RA"}
        self.cameras = {}
        self.preview_enabled = False
        self.stop_event = threading.Event()
        self.jobs = queue.Queue(maxsize=OCR_QUEUE_SIZE)
        self.results = queue.Queue()
        self.worker = None
        self.template_bank = UkNumberTemplateBank()

        self.states = {
            gate: {
                "active": False,
                "session": 0,
                "plate": None,
                "plate_time": 0.0,
                "last_present": 0.0,
                "votes": deque(maxlen=PLATE_VOTE_WINDOW),
                "attempts": 0,
                "in_flight": False,
                "last_submit": 0.0,
                "retry_after": 0.0,
                "best_frame": None,
                "best_score": -1.0,
                "select_started": 0.0,
                "error_count": 0,
                "debug_strip": None,
            }
            for gate in ("VAO", "RA")
        }

        for gate, cfg in self.config.items():
            try:
                self.cameras[gate] = CameraStream(cfg["device"], gate)
                print("[CAMERA] {} MO THANH CONG".format(gate), flush=True)
            except Exception as exc:
                self.cameras[gate] = None
                print("[CAMERA ERROR] {}: {}".format(gate, exc), flush=True)

        self.worker = threading.Thread(target=self._worker_loop, name="PLATE-TEMPLATE", daemon=True)
        self.worker.start()

    def open_preview_windows(self):
        if not globals().get("SHOW_CAMERA_WINDOWS", True):
            return
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            print("[CAMERA GUI] Khong co DISPLAY. Hay chay trong Raspberry Pi Desktop/VNC.", flush=True)
            return
        try:
            for gate in ("VAO", "RA"):
                name = self.window_names[gate]
                cv2.namedWindow(name, cv2.WINDOW_NORMAL)
                cv2.resizeWindow(name, PREVIEW_WIDTH, PREVIEW_HEIGHT)
                cv2.moveWindow(name, self.config[gate]["window_x"], self.config[gate]["window_y"])
            self.preview_enabled = True
            print("[CAMERA GUI] DA MO HAI CUA SO.", flush=True)
        except cv2.error as exc:
            self.preview_enabled = False
            print("[CAMERA GUI ERROR] {}".format(exc), flush=True)

    def _worker_loop(self):
        while not self.stop_event.is_set():
            try:
                job = self.jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if job is None:
                self.jobs.task_done()
                break

            gate, session, attempt, frame = job
            start = time.monotonic()
            try:
                plate, meta, strip = recognize_plate_debug(
                    frame, self.config[gate]["roi"], self.template_bank
                )
                meta = meta or {}
                raw = meta.get("raw", "")
                method = meta.get("method", "")
                confidence = float(meta.get("confidence", 0.0))
                error = None
            except Exception as exc:
                plate, raw, method, confidence, strip = "", "", "", 0.0, None
                error = "{}: {}".format(type(exc).__name__, exc)

            elapsed = time.monotonic() - start
            self.results.put((gate, session, attempt, plate, raw, method, confidence, strip, error, elapsed))
            self.jobs.task_done()

    def begin_session(self, gate, now=None):
        current = time.monotonic() if now is None else now
        st = self.states[gate]
        st["session"] += 1
        st["active"] = True
        st["plate"] = None
        st["plate_time"] = 0.0
        st["last_present"] = current
        st["votes"].clear()
        st["attempts"] = 0
        st["in_flight"] = False
        st["last_submit"] = 0.0
        st["retry_after"] = 0.0
        st["best_frame"] = None
        st["best_score"] = -1.0
        st["select_started"] = 0.0
        st["error_count"] = 0
        st["debug_strip"] = None
        cam = self.cameras.get(gate)
        if cam:
            cam.set_active_mode(True)
        print("[PLATE] BAT DAU PHIEN {}".format(gate), flush=True)

    def end_session(self, gate):
        st = self.states[gate]
        if st["active"]:
            print("[PLATE] KET THUC PHIEN {} | GIU BS={}".format(
                gate, st["plate"] or "CHUA NHAN DIEN"
            ), flush=True)
        st["active"] = False
        st["session"] += 1
        st["votes"].clear()
        st["best_frame"] = None
        st["best_score"] = -1.0
        st["in_flight"] = False
        cam = self.cameras.get(gate)
        if cam:
            cam.set_active_mode(False)

    def clear_plate(self, gate):
        st = self.states[gate]
        st["plate"] = None
        st["plate_time"] = 0.0
        st["votes"].clear()

    def update_presence(self, gate, present, now):
        st = self.states[gate]
        if present:
            st["last_present"] = now
            if not st["active"]:
                self.begin_session(gate, now)
        elif st["active"]:
            if (now - st["last_present"]) >= IR_HOLD_SECONDS and not st["in_flight"]:
                self.end_session(gate)

        if not st["active"] or st["plate"]:
            return
        if self.cameras.get(gate) is None or st["in_flight"] or now < st["retry_after"]:
            return
        if now - st["last_submit"] < PLATE_RETRY_INTERVAL:
            return
        if st["attempts"] >= PLATE_MAX_ATTEMPTS:
            st["attempts"] = 0
            st["votes"].clear()
            st["retry_after"] = now + PLATE_RETRY_PAUSE
            st["best_frame"] = None
            st["best_score"] = -1.0
            st["select_started"] = 0.0
            print("[TMPL {}] Het {} lan thu, thu lai sau {:.1f}s".format(
                gate, PLATE_MAX_ATTEMPTS, PLATE_RETRY_PAUSE
            ), flush=True)
            return

        frame, frame_time = self.cameras[gate].get_frame()
        if frame is None or now - frame_time > 0.8:
            return
        sharpness = frame_quality(frame, self.config[gate]["roi"])
        if st["best_frame"] is None:
            st["best_frame"] = frame
            st["best_score"] = sharpness
            st["select_started"] = now
        elif sharpness > st["best_score"]:
            st["best_frame"] = frame
            st["best_score"] = sharpness

        wait_elapsed = now - st["select_started"]
        if wait_elapsed < PLATE_CAPTURE_WINDOW:
            return
        if st["best_score"] < PLATE_MIN_SHARPNESS and wait_elapsed < PLATE_CAPTURE_WINDOW * 2.0:
            return

        attempt = st["attempts"] + 1
        try:
            self.jobs.put_nowait((gate, st["session"], attempt, st["best_frame"]))
            st["attempts"] = attempt
            st["in_flight"] = True
            st["last_submit"] = now
            print("[PLATE FRAME] {} | LAN {} | SHARPNESS={:.1f}".format(
                gate, attempt, st["best_score"]
            ), flush=True)
        except queue.Full:
            print("[TEMPLATE QUEUE] Hang doi day, bo qua frame.", flush=True)

        st["best_frame"] = None
        st["best_score"] = -1.0
        st["select_started"] = now

    def collect_results(self):
        while True:
            try:
                gate, session, attempt, plate, raw, method, confidence, strip, error, elapsed = self.results.get_nowait()
            except queue.Empty:
                break
            st = self.states[gate]
            if session != st["session"] or not st["active"]:
                continue
            st["in_flight"] = False
            if strip is not None:
                st["debug_strip"] = strip
            if error:
                st["error_count"] += 1
                print("[TEMPLATE ERROR] {}: {}".format(gate, error), flush=True)
                continue
            if not valid_plate(plate):
                print("[TMPL {}] LAN {} CHUA DAT [{}] | RAW={} | CONF={:.2f} | {:.1f}ms".format(
                    gate, attempt, method, raw, confidence, elapsed * 1000.0
                ), flush=True)
                continue

            st["votes"].append(plate)
            voted, agreement = vote_by_character_position(st["votes"], PLATE_MIN_MATCHES)
            print("[TMPL {}] [{}] DOC={} | CONF={:.2f} | PHIEU={} | DONG THUAN={:.0%} | {:.1f}ms".format(
                gate, method, plate, confidence, list(st["votes"]), agreement, elapsed * 1000.0
            ), flush=True)
            if voted and agreement >= PLATE_MIN_AGREEMENT:
                st["plate"] = voted
                st["plate_time"] = time.monotonic()
                cam = self.cameras.get(gate)
                if cam:
                    cam.set_active_mode(False)
                print("[PLATE CONFIRMED] {} = {}".format(gate, format_plate(voted)), flush=True)

    def get_plate(self, gate):
        return self.states[gate]["plate"]

    def get_plate_age(self, gate, now=None):
        st = self.states[gate]
        if not st["plate"] or st["plate_time"] <= 0:
            return None
        current = time.monotonic() if now is None else now
        return max(0.0, current - st["plate_time"])

    def status(self, gate):
        st = self.states[gate]
        if st["plate"]:
            return "DA NHAN DIEN"
        if st["active"] and st["attempts"]:
            return "DANG NHAN DIEN"
        if st["active"]:
            return "DANG QUET BIEN SO"
        if self.cameras.get(gate) is None:
            return "CAMERA LOI"
        return "CHO XE"

    @staticmethod
    def _error_frame(message):
        canvas = np.zeros((PREVIEW_HEIGHT, PREVIEW_WIDTH, 3), dtype=np.uint8)
        cv2.putText(canvas, message, (20, PREVIEW_HEIGHT // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
        return canvas

    def show_windows(self):
        if not self.preview_enabled:
            return
        now = time.monotonic()
        for gate in ("VAO", "RA"):
            name = self.window_names[gate]
            cam = self.cameras.get(gate)
            if cam is None:
                cv2.imshow(name, self._error_frame("CAMERA {} LOI".format(gate)))
                continue
            frame, frame_time = cam.get_frame()
            if frame is None:
                cv2.imshow(name, self._error_frame("DANG DOI HINH ANH"))
                continue
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = clamp_roi(self.config[gate]["roi"], w, h)
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
            cv2.putText(frame, "CONG {}".format(gate), (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
            plate = self.get_plate(gate)
            label = "BS: {}".format(format_plate(plate)) if plate else self.status(gate)
            cv2.putText(frame, label, (10, 62), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 0), 2)
            st = self.states[gate]
            if st["debug_strip"] is not None:
                strip = st["debug_strip"]
                sh, sw = strip.shape[:2]
                if sh + 10 < h and sw + 10 < w:
                    frame[5:5 + sh, w - sw - 5:w - 5] = strip
            if now - frame_time > 1.0:
                cv2.putText(frame, "HINH ANH CU", (10, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
            preview = cv2.resize(frame, (PREVIEW_WIDTH, PREVIEW_HEIGHT), interpolation=cv2.INTER_AREA)
            cv2.imshow(name, preview)
        key = cv2.waitKey(1) & 0xFF
        if key in (ord("q"), 27):
            raise KeyboardInterrupt("Dong cua so camera")

    def close(self):
        self.stop_event.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            try:
                self.jobs.get_nowait()
                self.jobs.task_done()
                self.jobs.put_nowait(None)
            except Exception:
                pass
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3.0)
        for gate, cam in self.cameras.items():
            if cam:
                try:
                    cam.close()
                    print("[CAMERA] DA DONG {}".format(gate), flush=True)
                except Exception as exc:
                    print("[CAMERA CLOSE ERROR] {}: {}".format(gate, exc), flush=True)
        if self.preview_enabled:
            try:
                cv2.destroyWindow(self.window_names["VAO"])
                cv2.destroyWindow(self.window_names["RA"])
                cv2.waitKey(1)
            except cv2.error:
                pass
        self.preview_enabled = False

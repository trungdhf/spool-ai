"""Per-spool drawings: one A3-landscape page per spool, cut from the source vector pages."""
import pymupdf

from .extract import _polys

A3 = pymupdf.paper_rect("a3-l")
COLORS = [(0.85, 0.1, 0.1), (0.1, 0.4, 0.9), (0.0, 0.6, 0.3), (0.85, 0.45, 0.0), (0.55, 0.2, 0.75), (0.0, 0.6, 0.7)]
FONT = "helv"


def _poly(page, geom, color, opacity=1.0, fill=True):
    geoms = _polys(geom)
    for g in geoms:
        pts = [pymupdf.Point(x, y) for x, y in g.exterior.coords]
        sh = page.new_shape()
        sh.draw_polyline(pts)
        sh.finish(color=color, fill=color if fill else None, width=0.4, fill_opacity=opacity, stroke_opacity=opacity,
                  closePath=True)
        sh.commit()


def color_of(i):
    return COLORS[i % len(COLORS)]


def _ensure_page(out, msg):
    """PyMuPDF cannot save a 0-page document; add a placeholder warning page."""
    if not out.page_count:
        pg = out.new_page(width=595, height=842)
        pg.insert_text((50, 60), msg, fontsize=13, color=(0.8, 0.1, 0.1))


def overview_pages(src_docs, sheets, spools):
    """Source sheets with every spool coloured (used for the web preview and the PDF cover)."""
    out = pymupdf.open()
    owner = {r: i for i, sp in enumerate(spools) for r in sp["runs"]}
    for sh in sheets:
        doc, pno = src_docs[sh.index]
        page = out.new_page(width=sh.width, height=sh.height)
        page.show_pdf_page(page.rect, doc, pno)
        for r in sh.runs:
            i = owner.get(r["id"])
            if i is not None:
                _poly(page, r["geom"].buffer(1.2), color_of(i), 0.85)
        for l in sh.labels:
            i = next((k for k, sp in enumerate(spools) if sp["name"] == l["spool"]), 0)
            page.draw_rect(pymupdf.Rect(l["x"] - 22, l["y"] - 10, l["x"] + 22, l["y"] + 10), color=color_of(i), width=1.5)
    _ensure_page(out, "NO SHEETS DETECTED - 図面の抽出に失敗しました")
    return out


def spool_pages(src_docs, sheets, spools, details=None):
    details = details or {}
    by_id = {r["id"]: (sh, r) for sh in sheets for r in sh.runs}
    out = pymupdf.open()
    for i, sp in enumerate(spools):
        col = color_of(i)
        page = out.new_page(width=A3.width, height=A3.height)
        frame = pymupdf.Rect(20, 20, A3.width - 20, A3.height - 20)
        page.draw_rect(frame, color=(0, 0, 0), width=1)
        side = pymupdf.Rect(frame.x1 - 300, frame.y0, frame.x1, frame.y1)
        page.draw_line(side.tl, side.bl, color=(0, 0, 0), width=0.8)
        area = pymupdf.Rect(frame.x0 + 8, frame.y0 + 8, side.x0 - 8, frame.y1 - 8)
        parts = {}
        for rid in sp["runs"]:
            sh, r = by_id[rid]
            parts.setdefault(sh.index, (sh, []))[1].append(r)
        n = max(1, len(parts))
        cells = [pymupdf.Rect(area.x0 + k * area.width / n, area.y0, area.x0 + (k + 1) * area.width / n, area.y1)
                 for k in range(n)]
        for cell, (sidx, (sh, runs)) in zip(cells, sorted(parts.items())):
            x0 = min(r["bbox"][0] for r in runs); y0 = min(r["bbox"][1] for r in runs)
            x1 = max(r["bbox"][2] for r in runs); y1 = max(r["bbox"][3] for r in runs)
            pad = 70
            raw = pymupdf.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) & pymupdf.Rect(0, 0, sh.width, sh.height)
            clip = raw & pymupdf.Rect(sh.width * 0.25, 0, sh.width, sh.height)
            if clip.is_empty or clip.width < 20 or clip.height < 20:
                clip = raw
            s = min((cell.width - 10) / clip.width, (cell.height - 40) / clip.height, 2.0)
            w, h = clip.width * s, clip.height * s
            tgt = pymupdf.Rect(cell.x0 + (cell.width - w) / 2, cell.y0 + 30, cell.x0 + (cell.width + w) / 2, cell.y0 + 30 + h)
            doc, pno = src_docs[sidx]
            src_page = doc[pno]
            if src_page.rotation:
                # clip must be in the source page's unrotated space; our coords are display space
                cs = [pymupdf.Point(x, y) * src_page.derotation_matrix
                      for x in (clip.x0, clip.x1) for y in (clip.y0, clip.y1)]
                clip_src = pymupdf.Rect(min(c.x for c in cs), min(c.y for c in cs),
                                        max(c.x for c in cs), max(c.y for c in cs))
            else:
                clip_src = clip
            page.show_pdf_page(tgt, doc, pno, clip=clip_src)

            def T(x, y):
                return tgt.x0 + (x - clip.x0) * s, tgt.y0 + (y - clip.y0) * s
            from shapely import affinity
            others = [r for r in sh.runs if r["id"] not in sp["runs"]]
            for r in others:
                g = affinity.affine_transform(r["geom"].buffer(1.5), [s, 0, 0, s, tgt.x0 - clip.x0 * s, tgt.y0 - clip.y0 * s])
                _poly(page, g, (0.78, 0.78, 0.78), 0.5)  # 他スプールは淡いグレーで残す
            for r in runs:
                g = affinity.affine_transform(r["geom"].buffer(0.6), [s, 0, 0, s, tgt.x0 - clip.x0 * s, tgt.y0 - clip.y0 * s])
                _poly(page, g, col, 0.9)
            for wd in sh.welds:
                if wd["no"] in sp["welds"]:
                    x, y = T(wd["x"], wd["y"])
                    page.draw_circle((x, y), 6, color=(0.9, 0, 0), width=1.4)
            page.insert_text((cell.x0 + 6, cell.y0 + 16), f"SHT {sh.title.get('sheet_no', sidx + 1)}  ({sh.title.get('drawing_no', '')})",
                             fontsize=10, fontname=FONT)
        # side panel
        y = side.y0 + 26
        page.insert_text((side.x0 + 12, y), sp["name"], fontsize=26, fontname="hebo", color=col)
        y += 22
        page.insert_text((side.x0 + 12, y), f"SPOOL DRAWING  /  SHT {', '.join(map(str, sp['sheets']))}", fontsize=10, fontname=FONT)
        det = details.get(sp["name"], {})
        rows = [("WELDS (shop)", ", ".join(map(str, sp["welds"])) or "-"),
                ("WELD COUNT", str(len(sp["welds"]))),
                ("ELBOWS (bends)", str(sp["bends"]))]
        if det.get("pipe_length_mm"):
            rows.append(("PIPE CUT LENGTH", f"{det['pipe_length_mm']} mm"))
        if det.get("db") is not None:
            rows.append(("WELD DB", str(det["db"])))
        y += 18
        for k, v in rows:
            page.insert_text((side.x0 + 12, y), k, fontsize=8, fontname=FONT, color=(0.35, 0.35, 0.35))
            y = _wrap(page, side.x0 + 120, y, v, 26, 9)
            y += 6
        y += 6
        page.insert_text((side.x0 + 12, y), "MATERIALS", fontsize=11, fontname="hebo")
        y += 14
        for it in det.get("bom", []):
            y = _wrap(page, side.x0 + 12, y, f"PT{it.get('pt', '')}  {it.get('desc', '')}  x{it.get('qty', '')}", 52, 8)
            y += 3
        notes = sp.get("needs_review", []) + det.get("notes", [])
        if notes:
            y += 8
            page.insert_text((side.x0 + 12, y), "要確認 / CHECK", fontsize=10, fontname="hebo", color=(0.8, 0.4, 0))
            y += 13
            for nte in notes:
                y = _wrap(page, side.x0 + 12, y, "- " + nte, 52, 8)
                y += 2
        page.insert_text((side.x0 + 12, side.y1 - 12), "Generated by Spool AI - check before fabrication", fontsize=7,
                         fontname=FONT, color=(0.4, 0.4, 0.4))
    _ensure_page(out, "NO SPOOLS DETECTED - スプールが検出できませんでした")
    return out


def _wrap(page, x, y, text, width, fs):
    words, line = str(text).split(), ""
    for w in words:
        if len(line) + len(w) + 1 > width and line:
            page.insert_text((x, y), line, fontsize=fs, fontname=FONT)
            y += fs + 2
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        page.insert_text((x, y), line, fontsize=fs, fontname=FONT)
    return y + fs - 2

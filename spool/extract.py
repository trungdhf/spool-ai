"""Vector extraction from CAD-exported isometric PDFs (one isometric per page).

Coordinates are PDF points, origin top-left (PyMuPDF)."""
import re
from dataclasses import dataclass, field

import pymupdf
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union


def _poly_points(d):
    pts = []
    for it in d["items"]:
        if it[0] == "l":
            pts += [it[1], it[2]]
        elif it[0] == "c":
            pts += [it[1], it[4]]
        elif it[0] == "re":
            r = it[1]
            pts += [r.tl, r.tr, r.br, r.bl]
        elif it[0] == "qu":
            q = it[1]
            pts += [q.ul, q.ur, q.lr, q.ll]
    out = []
    for p in pts:
        t = (round(p.x, 2), round(p.y, 2))
        if not out or out[-1] != t:
            out.append(t)
    return out


def _segments(d):
    for it in d["items"]:
        if it[0] == "l":
            yield (it[1].x, it[1].y), (it[2].x, it[2].y)
        elif it[0] == "c":
            yield (it[1].x, it[1].y), (it[4].x, it[4].y)
        elif it[0] == "re":
            r = it[1]
            c = [r.tl, r.tr, r.br, r.bl, r.tl]
            for a, b in zip(c, c[1:]):
                yield (a.x, a.y), (b.x, b.y)


@dataclass
class Sheet:
    index: int
    width: float
    height: float
    words: list = field(default_factory=list)
    pipes: list = field(default_factory=list)      # shapely polygons
    runs: list = field(default_factory=list)       # dicts
    gaps: list = field(default_factory=list)       # dicts
    labels: list = field(default_factory=list)     # SPL tags
    welds: list = field(default_factory=list)
    tags: list = field(default_factory=list)       # PT number tags (BOM refs)
    conts: list = field(default_factory=list)      # CONT. ON/FROM markers
    bom: list = field(default_factory=list)
    title: dict = field(default_factory=dict)
    thin: list = field(default_factory=list)       # thin line segments (leaders etc.)
    circles: list = field(default_factory=list)
    arrows: list = field(default_factory=list)


def _thickness(poly):
    return 2 * poly.area / poly.length if poly.length else 0


def extract_page(page, index):
    sh = Sheet(index=index, width=page.rect.width, height=page.rect.height)
    page_area = page.rect.width * page.rect.height
    drawings = page.get_drawings()
    for d in drawings:
        if d["type"] in ("f", "fs") and d["rect"].width * d["rect"].height < page_area * 0.5:
            pts = _poly_points(d)
            if len(pts) < 4:
                continue
            poly = Polygon(pts).buffer(0)
            if poly.is_empty or poly.area < 2:
                continue
            if len(pts) <= 5 and poly.area > 8:
                sh.arrows.append(poly)
            t = _thickness(poly)
            if 1.6 < t < 3.8 and poly.length > 10:
                sh.pipes.append(poly)
        if d["type"] in ("s", "fs"):
            items = d["items"]
            if items and all(i[0] == "c" for i in items) and len(items) >= 4:
                r = d["rect"]
                if 6 < r.width < 25 and abs(r.width - r.height) < 1.5:
                    sh.circles.append(((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2, r.width / 2))
                    continue
            for a, b in _segments(d):
                if (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 > 1:
                    sh.thin.append((a, b))

    sh.words = [dict(x0=w[0], y0=w[1], x1=w[2], y1=w[3], text=w[4].replace("\xad", "-"), block=w[5], line=w[6])
                for w in page.get_text("words")]
    _build_runs(sh)
    _find_texts(sh, page)
    _parse_bom(sh, page)
    _find_welds(sh)
    _find_labels(sh)
    _find_gaps(sh)
    return sh


def _build_runs(sh):
    merged = unary_union([p.buffer(0.8) for p in sh.pipes])
    geoms = list(merged.geoms) if hasattr(merged, "geoms") else [merged]
    for i, g in enumerate(geoms):
        parts = [p for p in sh.pipes if p.intersects(g)]
        # centerline endpoints: extreme points along the polygon's main axis
        hull = g.minimum_rotated_rectangle
        sh.runs.append(dict(id=f"S{sh.index + 1}-R{i + 1}", geom=g.buffer(-0.8) if g.buffer(-0.8).area else g,
                            area=g.area, bends=sum(1 for p in parts if len(p.exterior.coords) > 8),
                            bbox=g.bounds, hull=hull))
    for r in sh.runs:
        r["ends"] = _run_ends(r["geom"], sh.pipes)


def _run_ends(geom, pipes):
    """Free ends: vertices of member pipe polygons' short edges not touching other pipes."""
    members = [p for p in pipes if p.buffer(0.5).intersects(geom)]
    ends = []
    for p in members:
        coords = list(p.exterior.coords)[:-1]
        if len(coords) < 4:
            continue
        rect = p.minimum_rotated_rectangle
        rc = list(rect.exterior.coords)[:4]
        e = [((rc[k][0] + rc[(k + 1) % 4][0]) / 2, (rc[k][1] + rc[(k + 1) % 4][1]) / 2,
              Point(rc[k]).distance(Point(rc[(k + 1) % 4]))) for k in range(4)]
        e.sort(key=lambda t: t[2])
        for x, y, _ in e[:2]:
            pt = Point(x, y)
            if not any(o is not p and o.distance(pt) < 1.2 for o in members):
                ends.append((round(x, 1), round(y, 1)))
    return ends


def _find_texts(sh, page):
    lines = {}
    for w in sh.words:
        lines.setdefault((w["block"], w["line"]), []).append(w)
    sh.text_lines = []
    for ws in lines.values():
        ws.sort(key=lambda w: w["x0"])
        sh.text_lines.append(dict(text=" ".join(w["text"] for w in ws),
                                  x0=min(w["x0"] for w in ws), y0=min(w["y0"] for w in ws),
                                  x1=max(w["x1"] for w in ws), y1=max(w["y1"] for w in ws)))
    for tl in sh.text_lines:
        m = re.match(r"CONT\.?\s*(ON|FROM)", tl["text"])
        if m:
            nxt = [o for o in sh.text_lines if 0 < o["y0"] - tl["y0"] < 20 and abs(o["x0"] - tl["x0"]) < 3]
            ref = nxt[0]["text"] if nxt else ""
            ms = re.search(r"SHT\s*(\d+)", ref)
            sh.conts.append(dict(kind=m.group(1), sheet=int(ms.group(1)) if ms else None, ref=ref,
                                 x=(tl["x0"] + tl["x1"]) / 2, y=(tl["y0"] + tl["y1"]) / 2))
        m = re.match(r"SHT\s*(\d+)\s*OF\s*(\d+)", tl["text"])
        if m:
            sh.title.update(sheet_no=int(m.group(1)), sheet_total=int(m.group(2)))
    tb = [tl for tl in sh.text_lines if tl["y0"] > sh.height * 0.85 and tl["x0"] > sh.width * 0.8
          and "_" in tl["text"]]
    if tb:
        sh.title["drawing_no"] = tb[0]["text"]


def _parse_bom(sh, page):
    head = [w for w in sh.words if w["text"] == "QTY"]
    comp = [w for w in sh.words if w["text"] == "COMPONENT"]
    ns = [w for w in sh.words if w["text"] == "N.S."]
    if not head or not comp or not ns:
        return
    x_desc, x_ns, x_qty = comp[0]["x0"] - 1, ns[0]["x0"] - 1, head[0]["x0"] - 1
    x_right = x_qty + 60
    rows, section, cat = [], None, None
    tls = sorted([t for t in sh.text_lines if t["x0"] < x_right and t["y0"] > 20], key=lambda t: (round(t["y0"]), t["x0"]))
    words = sorted([w for w in sh.words if w["x0"] < x_right], key=lambda w: (round(w["y0"]), w["x0"]))
    ylines = {}
    for w in words:
        key = next((k for k in ylines if abs(k - w["y0"]) < 2), w["y0"])
        ylines.setdefault(key, []).append(w)
    cur = None
    for y in sorted(ylines):
        ws = ylines[y]
        txt = " ".join(w["text"] for w in ws)
        if "FABRICATION" in txt:
            section = "fabrication"; continue
        if "ERECTION" in txt:
            section = "erection"; continue
        if "PIPE SPOOLS" in txt:
            section = "spools"; continue
        if section == "spools":
            sh.title.setdefault("spools", []).extend(re.findall(r"SPL\d+", txt))
            continue
        if txt.startswith(("PT", "NO")) or "COMPONENT" in txt or txt in ("(MM)",):
            continue
        pt = [w for w in ws if w["x1"] < x_desc and w["text"].isdigit()]
        desc = " ".join(w["text"] for w in ws if x_desc <= w["x0"] < x_ns)
        nsv = " ".join(w["text"] for w in ws if x_ns <= w["x0"] < x_qty)
        qty = " ".join(w["text"] for w in ws if x_qty <= w["x0"])
        if pt and section in ("fabrication", "erection"):
            cur = dict(pt=int(pt[0]["text"]), section=section, category=cat, desc=desc, ns=nsv, qty=qty, _y=y)
            rows.append(cur)
        elif desc and not nsv and not qty and cur is not None and y - cur["_y"] < 14 and not cur.get("_more"):
            cur["desc"] += " " + desc
            cur["_more"] = True
        elif desc and not nsv and not qty:
            cat = desc
            cur = None
    for r in rows:
        r.pop("_y", None); r.pop("_more", None)
    sh.bom = rows


def _near_run(sh, x, y, maxd=12):
    best, bd = None, maxd
    p = Point(x, y)
    for r in sh.runs:
        d = r["geom"].distance(p)
        if d < bd:
            best, bd = r, d
    return best, bd


def _follow_leader(sh, start, maxlen=6, skip=None):
    """Follow thin segments from a point; return the far endpoint."""
    cur = start
    used = set()
    for _ in range(maxlen):
        nxt = None
        for k, (a, b) in enumerate(sh.thin):
            if k in used:
                continue
            for p, q in ((a, b), (b, a)):
                if (p[0] - cur[0]) ** 2 + (p[1] - cur[1]) ** 2 < 0.6:
                    nxt = (k, q)
                    break
            if nxt:
                break
        if not nxt:
            break
        used.add(nxt[0])
        cur = nxt[1]
    return cur


def _leader_target(sh, box, pad=2.5):
    """Thin segment that starts on the box border and points away: return its far end."""
    x0, y0, x1, y1 = box
    best = None
    for a, b in sh.thin:
        for p, q in ((a, b), (b, a)):
            on = (x0 - pad <= p[0] <= x1 + pad and y0 - pad <= p[1] <= y1 + pad)
            out = not (x0 - pad <= q[0] <= x1 + pad and y0 - pad <= q[1] <= y1 + pad)
            if on and out:
                L = ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5
                if L > 4 and (best is None or L > best[0]):
                    best = (L, p, q)
    if not best:
        return None
    return _follow_leader(sh, best[2])


def _boxed(sh, w):
    cy = (w["y0"] + w["y1"]) / 2
    def vert(xa, xb):
        return any(xa <= a[0] <= xb and abs(a[0] - b[0]) < 0.3 and min(a[1], b[1]) < cy < max(a[1], b[1])
                   and abs(a[1] - b[1]) < 25 for a, b in sh.thin)
    return vert(w["x0"] - 5, w["x0"] + 0.5) and vert(w["x1"] - 0.5, w["x1"] + 5)


def _find_welds(sh):
    for (cx, cy, r) in sh.circles:
        ws = [w for w in sh.words if w["text"].isdigit() and abs((w["x0"] + w["x1"]) / 2 - cx) < r
              and abs((w["y0"] + w["y1"]) / 2 - cy) < r]
        if not ws:
            continue
        no = int(ws[0]["text"])
        tgt = None
        for a, b in sh.thin:
            for p, q in ((a, b), (b, a)):
                dp = ((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5
                dq = ((q[0] - cx) ** 2 + (q[1] - cy) ** 2) ** 0.5
                if abs(dp - r) < 1.5 and dq > r + 3:
                    if tgt is None or dq > tgt[0]:
                        tgt = (dq, q)
        x, y = (tgt[1] if tgt else (cx, cy))
        run, d = _near_run(sh, x, y, 15)
        sh.welds.append(dict(no=no, x=round(x, 1), y=round(y, 1), cx=cx, cy=cy, run=run["id"] if run else None,
                             dist=round(d, 1)))
    sh.welds.sort(key=lambda w: w["no"])


def _find_labels(sh):
    for w in sh.words:
        m = re.fullmatch(r"SPL\d+", w["text"])
        box = (w["x0"], w["y0"], w["x1"], w["y1"])
        if m and w["x0"] > sh.width * 0.25:
            tgt = _leader_target(sh, box)
            run, d = _near_run(sh, *(tgt or ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)), 25)
            sh.labels.append(dict(spool=w["text"], x=(box[0] + box[2]) / 2, y=(box[1] + box[3]) / 2,
                                  tx=tgt[0] if tgt else None, ty=tgt[1] if tgt else None,
                                  run=run["id"] if run else None))
            continue
        if w["text"].isdigit() and w["x0"] > sh.width * 0.25 and (w["y1"] - w["y0"]) > 12 and _boxed(sh, w):
            # PT tag: number in a small square box, possibly followed by a word (ELBO, VALV, TEE)
            below = [o for o in sh.text_lines if 0 < o["y0"] - w["y0"] < 18 and abs(o["x0"] - w["x0"]) < 3]
            tgt = _leader_target(sh, (w["x0"] - 2, w["y0"] - 2, w["x1"] + 2, (below[0]["y1"] if below else w["y1"]) + 2))
            sh.tags.append(dict(pt=int(w["text"]), x=(w["x0"] + w["x1"]) / 2, y=(w["y0"] + w["y1"]) / 2,
                                word=below[0]["text"] if below else "", tx=tgt[0] if tgt else None,
                                ty=tgt[1] if tgt else None))
    for t in sh.tags:
        px, py = (t["tx"], t["ty"]) if t["tx"] is not None else (t["x"], t["y"])
        run, d = _near_run(sh, px, py, 20)
        t["run"] = run["id"] if run else None


def _find_gaps(sh):
    """Pairs of run ends facing each other (inline item, flange pair, flow arrow...)."""
    ends = [(r["id"], e) for r in sh.runs for e in r["ends"]]
    used = set()
    for i, (ra, ea) in enumerate(ends):
        best = None
        for j, (rb, eb) in enumerate(ends):
            if rb == ra or j in used or i in used:
                continue
            d = ((ea[0] - eb[0]) ** 2 + (ea[1] - eb[1]) ** 2) ** 0.5
            if d < 60 and (best is None or d < best[0]):
                best = (d, j, rb, eb)
        if best:
            used |= {i, best[1]}
            mx, my = (ea[0] + best[3][0]) / 2, (ea[1] + best[3][1]) / 2
            near_tags = [t for t in sh.tags if ((t["tx"] or t["x"]) - mx) ** 2 + ((t["ty"] or t["y"]) - my) ** 2 < 30 ** 2]
            near_txt = [tl["text"] for tl in sh.text_lines
                        if ((tl["x0"] + tl["x1"]) / 2 - mx) ** 2 + ((tl["y0"] + tl["y1"]) / 2 - my) ** 2 < 45 ** 2]
            sh.gaps.append(dict(id=f"S{sh.index + 1}-G{len(sh.gaps) + 1}", a=ra, b=best[2], length=round(best[0], 1),
                                ea=ea, eb=best[3],
                                x=round(mx, 1), y=round(my, 1), tags=[t["pt"] for t in near_tags],
                                nearby_text=near_txt[:8],
                                flow_arrow=any(a.distance(Point(mx, my)) < best[0] / 2 + 2 for a in sh.arrows)))


def extract(pdf_bytes, offset=0):
    doc = pymupdf.open("pdf", pdf_bytes)
    return [extract_page(p, offset + i) for i, p in enumerate(doc)], doc

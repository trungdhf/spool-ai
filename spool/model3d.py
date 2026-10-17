"""Iso drawing -> 3D centreline model.

Rules (same as the 配管アイソメ 3D-coordinate agent):
- X = E, Y = N, Z = up. Iso screen up-right = +E, up-left = +N, up = +Z.
- Direction comes from each segment's pixel direction (iso is NOT to scale);
  lengths come from the drawing's through dimensions (flange face -> elbow centre,
  centre -> centre). Segments without a dimension get an estimate flagged 要確認.
- Per-spool local origin: the first node of each spool = (0,0,0).
- A "joint" gap (welded ends) splits spools but keeps centreline continuity
  (+5 mm flange face gap). A "continuous" gap (flow arrow etc.) keeps 0 mm.
"""
import math

import pymupdf
from shapely.geometry import Point

# JIS-ish outside diameters (mm) for display
OD = {15: 21.7, 20: 27.2, 25: 34.0, 32: 42.7, 40: 48.6, 50: 60.5, 65: 76.3,
      80: 89.1, 90: 101.6, 100: 114.3, 125: 139.8, 150: 165.2, 200: 216.3,
      250: 267.4, 300: 318.5, 350: 355.6, 400: 406.4, 450: 457.2, 500: 508.0,
      550: 558.8, 600: 609.6}
AX = {"+E": (1, 0, 0), "-E": (-1, 0, 0), "+N": (0, 1, 0), "-N": (0, -1, 0),
      "+Z": (0, 0, 1), "-Z": (0, 0, -1)}
# screen directions in (x-right, y-up) coords
SCREEN = {"E": (math.cos(math.pi / 6), math.sin(math.pi / 6)),
          "N": (-math.cos(math.pi / 6), math.sin(math.pi / 6)),
          "Z": (0.0, 1.0)}
CLUSTER_TOL = 9.0          # px: endpoint + elbow-centroid merge distance
GAP_JOINT = 5.0            # mm: flange face gap across a welded joint gap
MAX_DEV_DEG = 12.0         # px direction vs iso axis deviation tolerated


def _d(p, q):
    return math.hypot(p[0] - q[0], p[1] - q[1])


def axis_from_px(p, q):
    """Nearest iso axis for the pixel direction p->q (pdf y-down). -> (axis, dev_deg)
    'SL' = sloped pipe (rolled offset): drawn direction matches no iso axis."""
    v = (q[0] - p[0], -(q[1] - p[1]))
    n = math.hypot(*v)
    if n < 1e-6:
        return "+Z", 90.0
    best = ("+Z", -2.0)
    for k, s in SCREEN.items():
        for sign in (1, -1):
            c = sign * (v[0] * s[0] + v[1] * s[1]) / n
            if c > best[1]:
                best = (("+" if sign > 0 else "-") + k, c)
    dev = round(math.degrees(math.acos(min(1.0, max(-1.0, best[1])))), 1)
    return ("SL" if dev > MAX_DEV_DEG else best[0]), dev


def _decode_slope(p, q):
    """Screen dir (pdf y-down) of a sloped pipe -> horizontal 3D unit vector
    (de, dn, 0). Iso projection: screen = (0.866*(de-dn), 0.5*(de+dn) + dz);
    with dz assumed 0 the plan azimuth is recoverable."""
    dx, dy = q[0] - p[0], -(q[1] - p[1])   # y-up screen
    L = math.hypot(dx, dy) or 1
    sx, sy = dx / L, dy / L
    de = sx / 1.732 + sy
    dn = sy - sx / 1.732
    n = math.hypot(de, dn) or 1
    return (round(de / n, 4), round(dn / n, 4), 0.0)


def _n_dirs(vecs, tol_deg=10.0):
    """Distinct line directions among outward unit vectors (sign-free)."""
    reps = []
    lim = math.cos(math.radians(tol_deg))
    for v in vecs:
        if not any(abs(v[0] * r[0] + v[1] * r[1] + v[2] * r[2]) > lim for r in reps):
            reps.append(v)
    return len(reps)


def _pipe_pieces(sh):
    """Straight pipe polys -> centreline segments; bent polys -> elbow points."""
    segs, elbows = [], []
    members_seen = set()
    for r in sh.runs:
        members = [p for p in sh.pipes if p.buffer(0.5).intersects(r["geom"])]
        for p in members:
            members_seen.add(id(p))
            coords = list(p.exterior.coords)[:-1]
            rect = p.minimum_rotated_rectangle
            if rect.geom_type != "Polygon":
                continue
            rc = list(rect.exterior.coords)[:4]
            e = [((rc[k][0] + rc[(k + 1) % 4][0]) / 2, (rc[k][1] + rc[(k + 1) % 4][1]) / 2,
                  Point(rc[k]).distance(Point(rc[(k + 1) % 4]))) for k in range(4)]
            e.sort(key=lambda t: t[2])
            a, b = e[0][:2], e[1][:2]
            if _d(a, b) < 2:
                continue
            if len(coords) > 8:        # bent shape -> elbow centre approx. = centroid
                c = p.centroid
                elbows.append((c.x, c.y))
            else:
                segs.append({"run": r["id"], "a": a, "b": b})
    return segs, elbows


def _cluster(points, tol):
    """Union-find merge of (x,y) points within tol. Returns list of clusters
    [{x,y, members:[i]}] in first-seen order."""
    n = len(points)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if _d(points[i], points[j]) < tol:
                pi, pj = find(i), find(j)
                if pi != pj:
                    parent[pj] = pi
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    out = []
    for idxs in groups.values():
        xs = [points[i][0] for i in idxs]
        ys = [points[i][1] for i in idxs]
        out.append({"x": sum(xs) / len(xs), "y": sum(ys) / len(ys), "members": idxs})
    return out


def _dims(page, sh):
    """Dimension candidates: numeric spans >=30mm in the drawing area, with the
    text direction (rotated dims follow the iso axes)."""
    dims = []
    d = page.get_text("dict")   # unrotated coords like get_drawings -> map to display space
    M = page.rotation_matrix if page.rotation else None
    M0 = pymupdf.Matrix(M.a, M.b, M.c, M.d, 0, 0) if M is not None else None  # vectors: no translation
    bom_x = sh.width * 0.24           # left columns = weld table / BOM on these sheets
    for block in d["blocks"]:
        for line in block.get("lines", []):
            dr = line.get("dir", (1, 0))
            if M0 is not None:
                dv = pymupdf.Point(*dr) * M0
                dr = (dv.x, dv.y)
            for sp in line.get("spans", []):
                t = sp["text"].strip().replace(",", "")
                if not t.isdigit():
                    continue
                v = int(t)
                if v < 30:
                    continue
                ox, oy = sp.get("origin", (sp["bbox"][0], sp["bbox"][3]))
                x0, y0, x1, y1 = sp["bbox"]
                if M is not None:
                    op = pymupdf.Point(ox, oy) * M
                    ox, oy = op.x, op.y
                    cs = [pymupdf.Point(x, y) * M for x in (x0, x1) for y in (y0, y1)]
                    x0, y0, x1, y1 = (min(c.x for c in cs), min(c.y for c in cs),
                                      max(c.x for c in cs), max(c.y for c in cs))
                cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
                if cx < bom_x or (cy > sh.height * 0.85 and cx > sh.width * 0.8):
                    continue
                if any((cx - c[0]) ** 2 + (cy - c[1]) ** 2 < (c[2] + 1) ** 2 for c in sh.circles):
                    continue          # circled weld number, not a dim
                dims.append({"v": v, "cx": cx, "cy": cy, "ox": ox, "oy": oy,
                             "dir": (dr[0], -dr[1]), "bbox": (x0, y0, x1, y1)})
    return dims


def _assign_dims(segs3d, dims):
    """For each centreline segment pick the dim nearest to it. The dim text's
    rotation is NOT reliable (this CAD prints it along the extension line), so
    matching is positional: the dim centre must project onto the segment span
    and sit within ~90 px perpendicular to the seg's line."""
    for s in segs3d:
        ax_, ay = s["a_px"], s["b_px"]
        L = _d(ax_, ay) or 1
        ux, uy = (ay[0] - ax_[0]) / L, (ay[1] - ax_[1]) / L   # pdf dir a->b
        cand = []
        for dm in dims:
            vx, vy = dm["cx"] - ax_[0], dm["cy"] - ax_[1]
            t = vx * ux + vy * uy          # projection along segment
            perp = abs(vx * uy - vy * ux)  # perpendicular distance
            if -0.15 * L <= t <= 1.15 * L and perp < 90:
                cand.append((perp, dm["v"]))
        if cand:
            cand.sort()
            s["length_mm"], s["source"], s["confidence"] = cand[0][1], f"寸法 {cand[0][1]}", "high"
    # estimate the rest with the per-axis median mm/px scale (sloped runs are
    # drawn foreshortened, so a global scale is wrong; fall back to Z's scale
    # because verticals are never foreshortened, then the global median)
    ratios = {}
    for s in segs3d:
        if s.get("confidence") == "high" and s["px_len"] > 5:
            ratios.setdefault(s["axis"][-1], []).append(s["length_mm"] / s["px_len"])
    scales = {k: sorted(v)[len(v) // 2] for k, v in ratios.items()}
    base = scales.get("Z") or (sorted(r for v in ratios.values() for r in v)[
        sum(len(v) for v in ratios.values()) // 2] if ratios else None)
    for s in segs3d:
        if "length_mm" not in s:
            scale = scales.get(s["axis"][-1]) or base
            if scale:
                s["length_mm"] = round(s["px_len"] * scale)
                s["source"], s["confidence"] = f"推定（縮尺 {scale:.1f}）", "要確認"
            else:
                s["length_mm"] = round(s["px_len"] * 10)
                s["source"], s["confidence"] = "推定（寸法なし）", "要確認"
    return scales


def _sheet_bore(sh):
    for b in sh.bom:
        if b.get("category") == "PIPE" and str(b.get("ns", "")).strip().isdigit():
            return int(str(b["ns"]).strip())
    return 0


def build(sheets, spools, docs):
    """sheets: extract output; spools: assemble output; docs: {sheet_index: (doc, page_no)}.
    Returns model dict with per-spool local coords and per-sheet assembled coords."""
    out = {"units": "mm", "axes": "X=E, Y=N, Z=up（各スプール最初のノード=原点）",
           "threejs_note": "Three.js(Y up): (x,y,z) = (X, Z, -Y)", "sheets": [], "spools": []}
    for sh in sheets:
        sh_bore = _sheet_bore(sh)
        page = docs[sh.index][0][docs[sh.index][1]] if docs.get(sh.index) else None
        segs, elbows = _pipe_pieces(sh)
        pts, ptag = [], []
        for s in segs:
            pts += [s["a"], s["b"]]
            ptag += [("seg", len(ptag)), ("seg", len(ptag))]
        eb0 = len(pts)
        pts += elbows
        ptag += [("elb", i) for i in range(len(elbows))]
        clusters = _cluster(pts, CLUSTER_TOL)
        node_of_pt = {}
        for ci, c in enumerate(clusters):
            for i in c["members"]:
                node_of_pt[i] = ci
        nodes = [{"id": f"N{ci + 1}", "x": c["x"], "y": c["y"], "ends": 0, "elbow": False,
                  "segs": [], "run": None} for ci, c in enumerate(clusters)]
        seg3d = []
        for si, s in enumerate(segs):
            na, nb = node_of_pt[2 * si], node_of_pt[2 * si + 1]
            if na == nb:
                continue
            ax, dev = axis_from_px(s["a"], s["b"])
            d3 = AX.get(ax) or _decode_slope(s["a"], s["b"])
            g = {"a": na, "b": nb, "a_px": s["a"], "b_px": s["b"], "run": s["run"],
                 "axis": ax, "dir3d": d3, "px_dev": dev, "px_len": _d(s["a"], s["b"]),
                 "kind": "pipe"}
            seg3d.append(g)
            nodes[na]["ends"] += 1
            nodes[nb]["ends"] += 1
            nodes[na]["segs"].append(len(seg3d) - 1)
            nodes[nb]["segs"].append(len(seg3d) - 1)
            for nci in (na, nb):
                nodes[nci]["run"] = nodes[nci]["run"] or s["run"]
        for i, _ in enumerate(elbows):
            ci = node_of_pt[eb0 + i]
            nodes[ci]["elbow"] = True
        # gap bridges: run ends that did not merge into a node get their own node
        def node_at(pt, run):
            best, bd = None, CLUSTER_TOL
            for i, n in enumerate(nodes):
                d = _d((n["x"], n["y"]), pt)
                if d < bd:
                    best, bd = i, d
            if best is None:
                nodes.append({"id": f"N{len(nodes) + 1}", "x": pt[0], "y": pt[1], "ends": 0,
                              "elbow": False, "segs": [], "run": run})
                best = len(nodes) - 1
            nodes[best]["run"] = nodes[best]["run"] or run
            return best
        for g in sh.gaps:
            na, nb = node_at(g["ea"], g["a"]), node_at(g["eb"], g["b"])
            ax, dev = axis_from_px(g["ea"], g["eb"])
            joint = g.get("kind") == "joint"
            seg3d.append({"a": na, "b": nb, "a_px": g["ea"], "b_px": g["eb"], "run": None,
                          "axis": ax, "dir3d": AX.get(ax) or _decode_slope(g["ea"], g["eb"]),
                          "px_dev": dev, "px_len": _d(g["ea"], g["eb"]),
                          "kind": "joint" if joint else "bridge",
                          "length_mm": GAP_JOINT if joint else 0.0,
                          "source": "フランジ面すき間" if joint else "連続",
                          "confidence": "high", "welds": g.get("welds", [])})
            nodes[na]["segs"].append(len(seg3d) - 1)
            nodes[nb]["segs"].append(len(seg3d) - 1)
        dims = _dims(page, sh) if page is not None else []
        _assign_dims([s for s in seg3d if s["kind"] == "pipe"], dims)

        # node types
        for i, n in enumerate(nodes):
            n["id"] = f"N{i + 1}"
            vecs = []
            for si in n["segs"]:
                s = seg3d[si]
                vecs.append(s["dir3d"] if s["a"] == i else tuple(-v for v in s["dir3d"]))
            lines = _n_dirs(vecs)
            if n["ends"] >= 3 or lines >= 3:
                n["type"] = "tee"
            elif n["elbow"] or lines >= 2:
                n["type"] = "elbow"
            elif n["ends"] == 1 or n["ends"] == 0:
                n["type"] = "end"
            else:
                n["type"] = "weld"
            # weld circle / PT tag near node -> ref
            ref = [w["no"] for w in sh.welds if _d((w["x"], w["y"]), (n["x"], n["y"])) < 9]
            tag = [t["pt"] for t in sh.tags if _d((t.get("tx") or t["x"], t.get("ty") or t["y"]), (n["x"], n["y"])) < 14]
            n["refs"] = [f"W{w}" for w in ref] + [f"PT{t}" for t in tag]
            if any(sg for sg in n["segs"] if seg3d[sg]["kind"] == "joint"):
                n["type"] = "flange" if n["type"] in ("end", "weld") else n["type"]
        out["sheets"].append({"index": sh.index, "sheet_no": sh.title.get("sheet_no"),
                              "file": sh.title.get("file"), "nodes": nodes, "segs": seg3d,
                              "dims_found": len(dims), "bore": sh_bore})

    # ---- solve coordinates: BFS per connected component per sheet ----
    run_to_spool = {}
    for sp in spools:
        for rid in sp["runs"]:
            run_to_spool[rid] = sp["name"]

    for shd in out["sheets"]:
        nodes, seg3d = shd["nodes"], shd["segs"]
        gpos = {}                     # node idx -> global (assembly) coords
        comp = {}                     # node idx -> component id
        cid = 0
        for start in range(len(nodes)):
            if start in comp:
                continue
            cid += 1
            comp[start] = cid
            gpos[start] = (0.0, 0.0, 0.0)
            stack = [start]
            while stack:
                cur = stack.pop()
                for si in nodes[cur]["segs"]:
                    s = seg3d[si]
                    nxt = s["b"] if s["a"] == cur else s["a"]
                    if nxt in comp:
                        continue
                    d = s["dir3d"] if s["a"] == cur else tuple(-v for v in s["dir3d"])
                    gpos[nxt] = tuple(round(gpos[cur][i] + d[i] * s["length_mm"], 1) for i in range(3))
                    comp[nxt] = cid
                    stack.append(nxt)
        shd["gpos"], shd["comp"] = gpos, comp
        # spread unconnected components along +E for the assembled view
        xmax = {}
        for i, c in comp.items():
            xmax[c] = max(xmax.get(c, -1e9), gpos[i][0])
        base = 0.0
        for c in sorted(xmax):
            if c == 1:
                base = xmax[c] + 800
                continue
            for i in [i for i, cc in comp.items() if cc == c]:
                gpos[i] = (round(gpos[i][0] + base, 1), gpos[i][1], gpos[i][2])
            base += xmax[c] + 800

    # per-spool output: local coords (root node = origin) + node/seg detail
    for sp in spools:
        sp_nodes, sp_segs, seen = [], [], set()
        for shd in out["sheets"]:
            for i, n in enumerate(shd["nodes"]):
                if n["run"] in sp["runs"]:
                    sp_nodes.append((shd["index"], i, n))
        root = next((i for _, i, n in sp_nodes if n["type"] in ("flange", "end")), None)
        if root is None and sp_nodes:
            root = sp_nodes[0][1]
        root_sh = next((si for si, i, n in sp_nodes if i == root), None)
        origin = out["sheets"][root_sh]["gpos"].get(root, (0, 0, 0)) if root_sh is not None else (0, 0, 0)
        bore = next((out["sheets"][si]["bore"] for si, i, n in sp_nodes if out["sheets"][si]["bore"]), 0)
        node_ids = {}
        outnodes = []
        for si, i, n in sp_nodes:
            shd = out["sheets"][si]
            node_ids[(si, i)] = f"N{len(outnodes) + 1}"
            g = shd["gpos"][i]
            local = tuple(round(g[k] - origin[k], 1) for k in range(3))
            outnodes.append({"id": node_ids[(si, i)], "type": n["type"], "refs": n["refs"],
                             "bore": bore, "p": list(local), "g": [round(v, 1) for v in g],
                             "sheet": shd["sheet_no"], "px": [round(n["x"], 1), round(n["y"], 1)]})
        for shd in out["sheets"]:
            for s in shd["segs"]:
                if s["kind"] != "pipe" or s["run"] not in sp["runs"]:
                    continue
                if (shd["index"], s["a"], s["b"]) in seen:
                    continue
                seen.add((shd["index"], s["a"], s["b"]))
                conf = s["confidence"]
                if s["px_dev"] > MAX_DEV_DEG:
                    conf = "要確認"
                sp_segs.append({"a": node_ids.get((shd["index"], s["a"])),
                                "b": node_ids.get((shd["index"], s["b"])),
                                "axis": s["axis"], "length_mm": s["length_mm"],
                                "source": s["source"], "confidence": conf,
                                "px_dev_deg": s["px_dev"]})
        unresolved = sum(1 for s in sp_segs if s["confidence"] == "要確認")
        out["spools"].append({"name": sp["name"], "status": "要確認" if unresolved or not sp_segs else "OK",
                              "nodes": outnodes, "segments": sp_segs, "bore": bore,
                              "od": OD.get(bore, 100)})
    # simplify sheet data for the assembled viewer
    for shd in out["sheets"]:
        shd["model"] = {
            "nodes": [{"id": i, "g": [round(v, 1) for v in shd["gpos"][i]], "type": n["type"],
                       "refs": n["refs"], "run": n["run"], "x": round(n["x"], 1), "y": round(n["y"], 1)}
                      for i, n in enumerate(shd["nodes"]) if i in shd["gpos"]],
            "segs": [{"a": s["a"], "b": s["b"], "axis": s["axis"], "length_mm": s["length_mm"],
                      "kind": s["kind"], "confidence": s["confidence"]} for s in shd["segs"]]}
        del shd["nodes"], shd["segs"], shd["gpos"], shd["comp"]
    return out


def _opp(axis):
    return ("-" if axis[0] == "+" else "+") + axis[1:]


def _oppv(axis):
    return tuple(-v for v in AX[axis])


def csv_3dmodeleditor(name, sp):
    """3DModelEditor (trungdhf/3DModelEditor) MyData-style rows per spool."""
    nd = {n["id"]: n for n in sp["nodes"]}
    lines = []
    for s in sp["segments"]:
        ln = next((l for l in lines if l["pts"][-1] == s["a"]), None)
        if ln is None:
            ln = {"main_no": len(lines), "pts": [s["a"]], "segs": []}
            lines.append(ln)
        ln["pts"].append(s["b"])
        ln["segs"].append(s)
    rows = ["No,MainNo,Type,Size,OD,X0,Y0,Z0,X,Y,Z,DX,DY,DZ,Length,From,To,Source,Confidence"]
    no = 0
    for ln in lines:
        for s in ln["segs"]:
            a, b = nd.get(s["a"]), nd.get(s["b"])
            if not a or not b:
                continue
            no += 1
            bore = s.get("bore") or sp["bore"]
            rows.append(",".join(str(v) for v in [
                no, ln["main_no"], round(s["length_mm"]), bore, OD.get(bore, 0),
                *a["p"], *b["p"],
                round(b["p"][0] - a["p"][0], 1), round(b["p"][1] - a["p"][1], 1),
                round(b["p"][2] - a["p"][2], 1), s["length_mm"],
                f'{s["a"]} {a["type"]}', f'{s["b"]} {b["type"]}', s["source"], s["confidence"]]))
    return "\n".join(rows) + "\n"


def pcf(name, sp):
    """ISOGEN-style PCF for one spool (local coords)."""
    nd = {n["id"]: n for n in sp["nodes"]}
    L = ["ISOGEN-FILES ISOGEN.FLS", "UNITS-BORE MM", "UNITS-CO-ORDS MM", "UNITS-WEIGHT KGS",
         "UNITS-BOLT-DIA MM", "UNITS-BOLT-LENGTH MM", f"PIPELINE-REFERENCE {name}",
         "    PIPING-SPEC B31"]
    f = lambda p, b: f"    END-POINT {p[0]:.1f} {p[1]:.1f} {p[2]:.1f} {b}"
    for s in sp["segments"]:
        a, b = nd.get(s["a"]), nd.get(s["b"])
        if not a or not b:
            continue
        bore = sp["bore"]
        L += ["PIPE", f(a["p"], bore), f(b["p"], bore)]
    for n in sp["nodes"]:
        bore = n["bore"] or sp["bore"]
        if n["type"] == "elbow":
            L += ["ELBOW", f(n["p"], bore), f(n["p"], bore),
                  f"    CENTRE-POINT {n['p'][0]:.1f} {n['p'][1]:.1f} {n['p'][2]:.1f} {bore}",
                  "    SKEY ELBW"]
        elif n["type"] == "tee":
            L += ["TEE", f(n["p"], bore), f(n["p"], bore),
                  f"    CENTRE-POINT {n['p'][0]:.1f} {n['p'][1]:.1f} {n['p'][2]:.1f} {bore}",
                  "    SKEY TERW"]
        elif n["type"] in ("flange", "end"):
            L += ["FLANGE", f(n["p"], bore), f(n["p"], bore), "    SKEY FLSO",
                  f"    ITEM-CODE {' '.join(n['refs']) or n['id']}"]
    return "\n".join(L) + "\n"

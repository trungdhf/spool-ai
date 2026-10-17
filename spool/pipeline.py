"""End-to-end: PDFs -> extraction -> spools -> AI review -> drawings."""
import base64

from .agent import review
from .assemble import assemble
from .draw import color_of, overview_pages, spool_pages
from .extract import extract
from .graph import cypher, push
from .model3d import build as build3d, csv_3dmodeleditor, pcf

A_TO_B = {15: 0.5, 20: 0.75, 25: 1, 32: 1.25, 40: 1.5, 50: 2, 65: 2.5, 80: 3, 90: 3.5, 100: 4, 125: 5, 150: 6}


def nominal_b(sheets):
    for sh in sheets:
        for b in sh.bom:
            if b["category"] == "PIPE" and b["ns"].strip().isdigit():
                a = int(b["ns"])
                return A_TO_B.get(a, a / 25)
    return None


def run(files, use_ai=True):
    sheets, docs = [], {}
    for name, data in files:
        shs, doc = extract(data, len(sheets))
        for k, sh in enumerate(shs):
            sh.title.setdefault("sheet_no", sh.index + 1)
            sh.title["file"] = name
            docs[sh.index] = (doc, k)
        sheets += shs
    sheets.sort(key=lambda s: s.title["sheet_no"])
    warnings = []
    if not sheets:
        warnings.append("図面（配管ライン）を抽出できませんでした。ベクター PDF か確認してください")
    spools = assemble(sheets)
    if sheets and not spools:
        warnings.append("スプールが検出できませんでした（PIPE SPOOLS 一覧 / スプール番号の引出線が見つからない可能性）")
    rv = review(sheets, spools) if use_ai else dict(provider=None, spools={}, summary_ja="AIオフ")
    b = nominal_b(sheets)
    details = {}
    for sp in spools:
        d = dict(rv.get("spools", {}).get(sp["name"], {}))
        if b:
            d["db"] = round(len(sp["welds"]) * b, 2)
        details[sp["name"]] = d
    cy = cypher(sheets, spools, details)
    try:
        m3 = build3d(sheets, spools, docs)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        m3 = dict(units="mm", spools=[], sheets=[])
    m3csv = {sp["name"]: csv_3dmodeleditor(sp["name"], sp) for sp in m3["spools"] if sp["segments"]}
    m3pcf = {sp["name"]: pcf(sp["name"], sp) for sp in m3["spools"] if sp["segments"]}
    try:
        neo = push(cy)
    except Exception as e:  # noqa: BLE001
        neo = dict(pushed=False, reason=str(e)[:200])
    ov = overview_pages(docs, sheets, spools)
    spdf = spool_pages(docs, sheets, spools, details)
    owner = {r: sp["name"] for sp in spools for r in sp["runs"]}
    return dict(
        sheets=[dict(sheet_no=sh.title["sheet_no"], file=sh.title["file"], drawing_no=sh.title.get("drawing_no"),
                     spool_list=sh.title.get("spools", []), bom=sh.bom,
                     gaps=[{k: g[k] for k in ("id", "a", "b", "kind", "welds", "x", "y")} for g in sh.gaps],
                     welds=[{k: w[k] for k in ("no", "x", "y", "run")} | {"spool": owner.get(w["run"])} for w in sh.welds],
                     png=_png(ov[i])) for i, sh in enumerate(sheets)],
        spools=[sp | {"color": "#%02x%02x%02x" % tuple(int(c * 255) for c in color_of(i)), "detail": details[sp["name"]],
                      "png": _png(spdf[i], 45)} for i, sp in enumerate(spools)],
        review={k: v for k, v in rv.items() if k != "spools"},
        warnings=warnings,
        weld_b=b,
        neo4j=neo,
        cypher="\n".join(cy),
        model3d=m3,
        model3d_csv=m3csv,
        model3d_pcf=m3pcf,
        spools_pdf=base64.b64encode(spdf.tobytes(garbage=3, deflate=True)).decode(),
        overview_pdf=base64.b64encode(ov.tobytes(garbage=3, deflate=True)).decode())


def _png(page, dpi=50):
    return base64.b64encode(page.get_pixmap(dpi=dpi).tobytes("png")).decode()

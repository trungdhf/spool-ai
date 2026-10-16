"""DXF (from LibreDWG dwg2dxf) -> PDF with vector geometry and a real text layer,
like an AutoCAD plot with TTF fonts. Usage: python dwg_to_pdf.py in.dxf out.pdf"""
import sys

import pymupdf
from ezdxf import bbox, recover
from ezdxf.addons.drawing import Frontend, RenderContext, config, layout
from ezdxf.addons.drawing import pymupdf as pmb

MM = 72 / 25.4
MARGIN = 5
FONT = "/usr/share/fonts/truetype/liberation/LiberationSansNarrow-Regular.ttf"
CAP = 0.729


def convert(dxf_path, pdf_path):
    doc, _ = recover.readfile(dxf_path)
    msp = doc.modelspace()
    texts = []
    for t in list(msp.query("TEXT")):
        texts.append((t.dxf.text, t.dxf.insert, t.dxf.height,
                      t.dxf.get("rotation", 0), t.dxf.get("width", 1.0)))
        msp.delete_entity(t)
    ext = bbox.extents(msp)
    be = pmb.PyMuPdfBackend()
    cfg = config.Configuration(background_policy=config.BackgroundPolicy.WHITE,
                               color_policy=config.ColorPolicy.BLACK, min_lineweight=0.18)
    Frontend(RenderContext(doc), be, config=cfg).draw_layout(msp)
    pdf = be.get_pdf_bytes(layout.Page(0, 0, layout.Units.mm, margins=layout.Margins.all(MARGIN)),
                           settings=layout.Settings(fit_page=False, scale=1))
    out = pymupdf.open("pdf", pdf)
    page = out[0]
    page.insert_font(fontname="nar", fontfile=FONT)
    x0, y1 = ext.extmin.x, ext.extmax.y
    for text, p, h, rot, sx in texts:
        s = text.strip()
        if not s:
            continue
        pt = pymupdf.Point((p.x - x0 + MARGIN) * MM, (y1 - p.y + MARGIN) * MM)
        m = pymupdf.Matrix(sx, 0, 0, 1, 0, 0) * pymupdf.Matrix(-rot)
        page.insert_text(pt, s, fontsize=h * MM / CAP, fontname="nar", morph=(pt, m))
    out.save(pdf_path, garbage=3, deflate=True)


if __name__ == "__main__":
    convert(sys.argv[1], sys.argv[2])

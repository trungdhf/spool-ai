"""AI review agent: allocates sheet BOM + pipe length to spools and checks the split.
OpenAI-compatible chat API: ai& (AIAND_API_KEY) first, Vertex Gemini (GCP_SA_KEY_JSON) as fallback."""
import base64
import json
import os
import time
import urllib.request

AIAND_MODEL = os.environ.get("AIAND_MODEL", "deepseek-ai/deepseek-v4-flash")
VERTEX_MODEL = os.environ.get("VERTEX_MODEL", "google/gemini-3.5-flash")

SYSTEM = """You are a piping fabrication engineer. You receive vector data extracted from isometric drawing sheets
(pipe runs already grouped into shop spools by deterministic rules) plus each sheet's BOM.
Tasks:
1. For each spool, allocate the FABRICATION materials (pipe, elbows, tees, flanges...) of the sheets it lies on.
   Use bends (= elbows), weld numbers, nearby text (ELBO, TEE, F3/F4 flange tags, VALV) and dimension texts.
   Erection items (gaskets, bolts, valves, instruments) are NOT part of a spool; list them under erection.
2. Estimate pipe cut length per spool (mm) from the dimension texts near its runs; the sum per sheet should match
   the sheet BOM pipe quantity. If unsure, say so in notes.
3. Check the split: flags for anything a fabricator must verify (要確認), in Japanese, short.
Return JSON only: {"spools":{"SPL2":{"bom":[{"sheet":1,"pt":2,"desc":"...","qty":3}],"pipe_length_mm":1234,
"notes":["..."]}},"erection":[{"sheet":1,"pt":4,"desc":"...","qty":4}],"summary_ja":"..."}"""


def _http(url, body, headers, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def _vertex_token(sa):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64(b):
        return base64.urlsafe_b64encode(b).rstrip(b"=")
    now = int(time.time())
    h = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    c = b64(json.dumps({"iss": sa["client_email"], "scope": "https://www.googleapis.com/auth/cloud-platform",
                        "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(sa["private_key"].encode(), None)
    sig = b64(key.sign(h + b"." + c, padding.PKCS1v15(), hashes.SHA256()))
    data = f"grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion={(h + b'.' + c + b'.' + sig).decode()}"
    return json.loads(urllib.request.urlopen("https://oauth2.googleapis.com/token", data=data.encode()).read())["access_token"]


def provider():
    if os.environ.get("AIAND_API_KEY"):
        return "ai&", AIAND_MODEL
    if os.environ.get("GCP_SA_KEY_JSON"):
        return "vertex", VERTEX_MODEL
    return None, None


def chat(messages):
    name, model = provider()
    body = {"model": model, "messages": messages, "response_format": {"type": "json_object"}, "temperature": 0.1}
    if name == "ai&":
        base = os.environ.get("AIAND_BASE_URL", "https://api.aiand.com/v1")
        r = _http(base + "/chat/completions", body, {"Authorization": "Bearer " + os.environ["AIAND_API_KEY"]})
    elif name == "vertex":
        sa = json.loads(os.environ["GCP_SA_KEY_JSON"])
        url = (f"https://aiplatform.googleapis.com/v1/projects/{sa['project_id']}/locations/global/"
               "endpoints/openapi/chat/completions")
        r = _http(url, body, {"Authorization": "Bearer " + _vertex_token(sa)})
    else:
        return None
    txt = r["choices"][0]["message"]["content"].strip()
    if txt.startswith("```"):
        txt = txt.strip("`").split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(txt)


def facts(sheets, spools):
    out = {"sheets": [], "spools": spools}
    for sh in sheets:
        dims = [t["text"] for t in sh.text_lines if t["text"].isdigit() and t["x0"] > sh.width * 0.25]
        out["sheets"].append(dict(sheet=sh.title.get("sheet_no"), drawing=sh.title.get("drawing_no"),
                                  spool_list=sh.title.get("spools"), bom=sh.bom,
                                  runs=[dict(id=r["id"], bends=r["bends"],
                                             nearby_text=[t["text"] for t in sh.text_lines
                                                          if r["geom"].distance(_pt(t)) < 45][:25]) for r in sh.runs],
                                  gaps=[{k: g[k] for k in ("a", "b", "kind", "welds", "nearby_text")} for g in sh.gaps],
                                  dimension_texts=dims))
    return out


def _pt(t):
    from shapely.geometry import Point
    return Point((t["x0"] + t["x1"]) / 2, (t["y0"] + t["y1"]) / 2)


def review(sheets, spools):
    name, model = provider()
    if not name:
        return dict(provider=None, spools={}, erection=[], summary_ja="AI未設定（ルールのみで分割）")
    f = facts(sheets, spools)
    res = chat([{"role": "system", "content": SYSTEM},
                {"role": "user", "content": json.dumps(f, ensure_ascii=False, default=str)}]) or {}
    res["provider"] = f"{name} / {model}"
    res["checks"] = check(sheets, res)
    return res


def check(sheets, res):
    """Deterministic cross-check: allocated fabrication qty per sheet+pt must equal the sheet BOM."""
    alloc = {}
    for sp in res.get("spools", {}).values():
        for it in sp.get("bom", []):
            k = (it.get("sheet"), it.get("pt"))
            try:
                alloc[k] = alloc.get(k, 0) + float(str(it.get("qty", 0)).replace("MM", "").replace("mm", ""))
            except ValueError:
                pass
    out = []
    for sh in sheets:
        no = sh.title.get("sheet_no")
        for b in sh.bom:
            if b["section"] != "fabrication":
                continue
            want = float("".join(c for c in b["qty"] if c.isdigit() or c == ".") or 0)
            got = alloc.get((no, b["pt"]), 0)
            out.append(dict(sheet=no, pt=b["pt"], desc=b["desc"][:40], bom=want, allocated=got, ok=abs(want - got) < 1e-6))
    return out

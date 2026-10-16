"""Piping graph for Neo4j: Sheet / PipeRun / Weld / Joint / Spool / BOMItem."""
import base64
import json
import os
import urllib.request


def _q(v):
    return json.dumps(v, ensure_ascii=False)


def cypher(sheets, spools, details):
    owner = {r: sp["name"] for sp in spools for r in sp["runs"]}
    L = ["MATCH (n) WHERE n:Sheet OR n:PipeRun OR n:Weld OR n:Joint OR n:Spool OR n:BOMItem DETACH DELETE n;"]
    for sp in spools:
        d = details.get(sp["name"], {})
        L.append(f"MERGE (s:Spool {{name:{_q(sp['name'])}}}) SET s.welds={len(sp['welds'])}, s.bends={sp['bends']}, "
                 f"s.db={_q(d.get('db'))}, s.pipe_length_mm={_q(d.get('pipe_length_mm'))};")
    for sh in sheets:
        no = sh.title["sheet_no"]
        L.append(f"MERGE (:Sheet {{no:{no}, drawing:{_q(sh.title.get('drawing_no'))}, file:{_q(sh.title.get('file'))}}});")
        for r in sh.runs:
            L.append(f"MATCH (sh:Sheet {{no:{no}}}) MERGE (r:PipeRun {{id:{_q(r['id'])}}}) SET r.bends={r['bends']} "
                     f"MERGE (sh)-[:CONTAINS]->(r);")
            if r["id"] in owner:
                L.append(f"MATCH (r:PipeRun {{id:{_q(r['id'])}}}),(s:Spool {{name:{_q(owner[r['id']])}}}) MERGE (r)-[:PART_OF]->(s);")
        for w in sh.welds:
            L.append(f"MATCH (sh:Sheet {{no:{no}}}) MERGE (w:Weld {{no:{w['no']}}}) SET w.x={w['x']}, w.y={w['y']}, w.type='shop' "
                     f"MERGE (sh)-[:CONTAINS]->(w)" + (f" WITH w MATCH (r:PipeRun {{id:{_q(w['run'])}}}) MERGE (r)-[:HAS_WELD]->(w)" if w["run"] else "") + ";")
        for g in sh.gaps:
            rel = "CONNECTED_TO" if g["kind"] == "continuous" else "JOINED_BY_FLANGE"
            L.append(f"MATCH (a:PipeRun {{id:{_q(g['a'])}}}),(b:PipeRun {{id:{_q(g['b'])}}}) MERGE (a)-[:{rel} {{welds:{_q(g.get('welds', []))}}}]->(b);")
        for b in sh.bom:
            L.append(f"MATCH (sh:Sheet {{no:{no}}}) MERGE (i:BOMItem {{sheet:{no}, pt:{b['pt']}}}) SET i.desc={_q(b['desc'])}, "
                     f"i.ns={_q(b['ns'])}, i.qty={_q(b['qty'])}, i.section={_q(b['section'])} MERGE (sh)-[:HAS_BOM]->(i);")
    for sp in spools:
        for c in sp["conts"]:
            if c.get("to"):
                a, b = (c["sheet"], c["to"]) if c["kind"] == "ON" else (c["to"], c["sheet"])
                L.append(f"MATCH (a:Sheet {{no:{a}}}),(b:Sheet {{no:{b}}}) MERGE (a)-[:CONTINUES_TO]->(b);")
        for it in details.get(sp["name"], {}).get("bom", []):
            L.append(f"MATCH (s:Spool {{name:{_q(sp['name'])}}}),(i:BOMItem {{sheet:{_q(it.get('sheet'))}, pt:{_q(it.get('pt'))}}}) "
                     f"MERGE (s)-[:USES {{qty:{_q(str(it.get('qty')))}}}]->(i);")
    return L


def push(statements):
    """Neo4j Query API (Aura): NEO4J_URI=neo4j+s://xxxx.databases.neo4j.io, NEO4J_USER, NEO4J_PASSWORD."""
    uri = os.environ.get("NEO4J_URI")
    if not uri:
        return dict(pushed=False, reason="NEO4J_URI not set")
    host = uri.split("://", 1)[-1].rstrip("/")
    url = os.environ.get("NEO4J_QUERY_URL") or f"https://{host}/db/{os.environ.get('NEO4J_DATABASE', 'neo4j')}/query/v2"
    auth = base64.b64encode(f"{os.environ.get('NEO4J_USER', 'neo4j')}:{os.environ['NEO4J_PASSWORD']}".encode()).decode()
    for st in statements:
        req = urllib.request.Request(url, data=json.dumps({"statement": st}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Basic " + auth})
        urllib.request.urlopen(req, timeout=30).read()
    return dict(pushed=True, statements=len(statements))

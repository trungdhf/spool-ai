"""Group pipe runs into fabrication spools (deterministic first pass)."""
import re


def _d(p, q):
    return ((p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2) ** 0.5


def classify_gaps(sh, weld_r=9):
    for g in sh.gaps:
        welded = [w["no"] for w in sh.welds if min(_d((w["x"], w["y"]), g["ea"]), _d((w["x"], w["y"]), g["eb"])) < weld_r]
        g["welds"] = welded
        if welded:
            g["kind"] = "joint"          # flange pair / inline item between welded ends -> spool break
        else:
            g["kind"] = "continuous"     # pipe line interrupted only by a drawing symbol (flow arrow, dim)


def assemble(sheets):
    parent = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    for sh in sheets:
        classify_gaps(sh)
        for r in sh.runs:
            find(r["id"])
        for g in sh.gaps:
            if g["kind"] == "continuous":
                union(g["a"], g["b"])
    groups = {}
    for sh in sheets:
        for r in sh.runs:
            groups.setdefault(find(r["id"]), []).append((sh, r))
    labelled = {}
    for root, members in groups.items():
        names = sorted({l["spool"] for sh, r in members for l in sh.labels if l["run"] == r["id"]})
        labelled[root] = names
    spools = {}
    unnamed = 0
    for root, members in groups.items():
        names = labelled[root]
        if names:
            name = names[0]
        else:
            unnamed += 1
            name = f"UNLABELLED-{unnamed}"
        sp = spools.setdefault(name, dict(name=name, runs=[], sheets=[], welds=[], bends=0, conflicts=[],
                                          conts=[], needs_review=[]))
        if len(names) > 1:
            sp["conflicts"].append(f"group has several labels {names}")
        for sh, r in members:
            sp["runs"].append(r["id"])
            sp["bends"] += r["bends"]
            no = sh.title.get("sheet_no", sh.index + 1)
            if no not in sp["sheets"]:
                sp["sheets"].append(no)
            sp["welds"] += [w["no"] for w in sh.welds if w["run"] == r["id"]]
            for c in sh.conts:
                if any(_d((c["x"], c["y"]), e) < 90 for e in r["ends"]):
                    sp["conts"].append(dict(sheet=no, kind=c["kind"], to=c["sheet"]))
        if name.startswith("UNLABELLED"):
            sp["needs_review"].append("no SPL label found for this pipe run")
    for sp in spools.values():
        sp["welds"] = sorted(set(sp["welds"]))
        sp["sheets"].sort()
    for sh in sheets:
        listed = set(sh.title.get("spools", []))
        found = {n for n, sp in spools.items() if sh.title.get("sheet_no", sh.index + 1) in sp["sheets"]}
        for miss in listed - found:
            spools.setdefault(miss, dict(name=miss, runs=[], sheets=[], welds=[], bends=0, conflicts=[], conts=[],
                                         needs_review=["listed in PIPE SPOOLS but no pipe run found"]))
    return sorted(spools.values(), key=lambda s: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", s["name"])])

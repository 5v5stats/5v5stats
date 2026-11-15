"""Decode the React Server Components payload that Next.js embeds in 5v5stats pages.

Pages carry their content as `self.__next_f.push([1,"..."])` chunks; joined, they form lines like
`1d:["$","div",null,{...}]`. An element is ["$", tag, key, props]; props["children"] holds nested content.
"""
import json
import re

CHUNK = re.compile(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>', re.S)
LINE = re.compile(r"^([0-9a-f]+):(.*)$")


def payload(html):
    return "".join(json.loads('"' + c + '"') for c in CHUNK.findall(html))


REF = re.compile(r"^\$L?([0-9a-f]+)$")


def rows(html):
    """{ref: decoded JSON} for every line of the payload that holds JSON (elements, arrays, objects),
    with references to other lines ("$L19", "$1a") replaced by those lines' content."""
    out = {}
    for line in payload(html).split("\n"):
        m = LINE.match(line)
        if not m:
            continue
        body = m.group(2)
        if body[:1] in "[{":
            try:
                out[m.group(1)] = json.loads(body)
            except ValueError:
                pass

    def resolve(x, depth=0):
        if depth > 60:
            return x
        if isinstance(x, str):
            m = REF.match(x)
            if m and m.group(1) in out and not (isinstance(out[m.group(1)], dict) and "P" in out[m.group(1)]):
                return resolve(out[m.group(1)], depth + 1)
            return x
        if isinstance(x, list):
            return [resolve(y, depth + 1) for y in x]
        if isinstance(x, dict):
            return {k: resolve(v, depth + 1) for k, v in x.items()}
        return x
    return {k: resolve(v) for k, v in out.items()}


def main_tree(rws):
    """The page body: the biggest element tree once references are resolved (everything else is a part of it)."""
    trees = [v for v in rws.values() if isinstance(v, list)]
    return max(trees, key=lambda v: len(json.dumps(v)), default=[])


def is_el(x):
    return isinstance(x, list) and len(x) == 4 and x[0] == "$" and isinstance(x[1], str)


def walk(x):
    """Every element in a tree, depth first."""
    if is_el(x):
        yield x
        yield from walk(x[3].get("children") if isinstance(x[3], dict) else None)
    elif isinstance(x, list):
        for y in x:
            yield from walk(y)


def text(x):
    """Visible text of a tree, joined."""
    if x is None or x is True or x is False:
        return ""
    if isinstance(x, (str, int, float)):
        s = str(x)
        return "" if s.startswith("$") and len(s) < 6 else s
    if is_el(x):
        return text(x[3].get("children") if isinstance(x[3], dict) else None)
    if isinstance(x, list):
        return "".join(text(y) for y in x)
    return ""


def cells(tr):
    """Children of a <tr> as a list of <td> elements (flattening nested arrays)."""
    out = []

    def add(x):
        if is_el(x) and x[1] == "td":
            out.append(x)
        elif isinstance(x, list) and not is_el(x):
            for y in x:
                add(y)
    add(tr[3].get("children"))
    return out


def links(x, prefix):
    """(href, text) of links whose href starts with prefix."""
    return [(e[3]["href"], text(e)) for e in walk(x)
            if isinstance(e[3], dict) and isinstance(e[3].get("href"), str) and e[3]["href"].startswith(prefix)]


def num(s):
    s = str(s).strip().replace("%", "").replace("$", "").replace(",", "")
    if s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[1]) == 3 and s.replace(".", "").isdigit()):
        s = s.replace(".", "")  # Turkish thousands separator: 852.175 -> 852175
    try:
        return float(s) if "." in s else int(s)
    except ValueError:
        return None

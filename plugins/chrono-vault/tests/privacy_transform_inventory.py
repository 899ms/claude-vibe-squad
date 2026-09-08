"""Conservative AST inventory of operations receiving screened data.

Resolves local functions and imports, propagates assignments, collection writes,
arguments and returns to a fixed point. Branches are merged, so rows are candidate
consumers, not findings. Dynamic dispatch and external library internals require
manual tracing; this is not a proof about arbitrary Python programs.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

SEEDS = {"privacy.redact_text", "privacy.redact_fields"}


def inventory(sources):
    functions, imports = {}, {}
    for module, source in sources.items():
        tree = ast.parse(source)
        imports[module] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for name in node.names:
                    imports[module][name.asname or name.name] = f"{node.module}.{name.name}"
            elif isinstance(node, ast.Import):
                for name in node.names:
                    imports[module][name.asname or name.name] = name.name
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node in tree.body:
                functions[f"{module}.{node.name}"] = node
    names = {key: set() for key in functions}
    returns = set(SEEDS)
    rows = set()

    def resolve(module, expression):
        value = ast.unparse(expression)
        head, dot, rest = value.partition(".")
        if head in imports[module]:
            return imports[module][head] + (dot + rest if dot else "")
        return f"{module}.{value}" if f"{module}.{value}" in functions else value

    def marked(node, tainted, module):
        if isinstance(node, ast.Name):
            return node.id in tainted
        if isinstance(node, ast.Call) and resolve(module, node.func) in returns:
            return True
        return any(marked(child, tainted, module) for child in ast.iter_child_nodes(node))

    def targets(node):
        if isinstance(node, ast.Name):
            return {node.id}
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            return targets(node.value)
        if isinstance(node, (ast.Tuple, ast.List)):
            return set().union(*(targets(item) for item in node.elts))
        return set()

    changed = True
    while changed:
        before = (sum(map(len, names.values())), len(returns), len(rows))
        for key, function in functions.items():
            module = key.split(".")[0]
            tainted = names[key]
            for node in ast.walk(function):
                if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr, ast.AugAssign)):
                    if node.value is not None and marked(node.value, tainted, module):
                        for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                            tainted.update(targets(target))
                if isinstance(node, (ast.For, ast.comprehension)) and marked(node.iter, tainted, module):
                    tainted.update(targets(node.target))
                if isinstance(node, ast.Call):
                    arguments = [*node.args, *(item.value for item in node.keywords)]
                    callee = resolve(module, node.func)
                    if callee in functions:
                        parameters = [*functions[callee].args.posonlyargs, *functions[callee].args.args]
                        for parameter, argument in zip(parameters, node.args):
                            if marked(argument, tainted, module):
                                names[callee].add(parameter.arg)
                        for item in node.keywords:
                            if item.arg and marked(item.value, tainted, module):
                                names[callee].add(item.arg)
                    if isinstance(node.func, ast.Attribute) and node.func.attr in {"append", "extend", "update", "add"}:
                        if any(marked(arg, tainted, module) for arg in arguments):
                            tainted.update(targets(node.func.value))
                if isinstance(node, ast.Return) and node.value and marked(node.value, tainted, module):
                    returns.add(key)
                operation = None
                if isinstance(node, ast.Call) and any(marked(arg, tainted, module) for arg in [node.func, *node.args, *(k.value for k in node.keywords)]):
                    operation = "call:" + resolve(module, node.func)
                elif isinstance(node, ast.JoinedStr) and marked(node, tainted, module):
                    operation = "format:f-string"
                elif isinstance(node, ast.BinOp) and marked(node, tainted, module):
                    operation = "binary:" + type(node.op).__name__
                elif isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice) and marked(node.value, tainted, module):
                    operation = "slice"
                elif isinstance(node, (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)) and marked(node, tainted, module):
                    operation = "collection:" + type(node).__name__
                if operation:
                    rows.add((key, node.lineno, operation))
        changed = before != (sum(map(len, names.values())), len(returns), len(rows))
    return sorted(rows)


def self_check():
    positive = {"privacy": "def redact_text(value): return value", "probe": '''
from privacy import redact_text as screen
def consumer(value):
    return value.replace(' ', '-')[:120]
def source(value):
    screened = screen(value)
    alias = screened
    return consumer(alias)
'''}
    rows = inventory(positive)
    assert ("probe.consumer", 4, "call:value.replace") in rows, rows
    assert ("probe.consumer", 4, "slice") in rows, rows
    assert inventory({"probe": "def consumer(value): return value.replace(' ', '-')"}) == []


def main():
    self_check()
    root = Path(__file__).resolve().parents[1]
    sources = {p.stem: p.read_text() for p in sorted(root.glob("*.py"))}
    rows = inventory(sources)
    # The real, previously confirmed consumer is a second positive control.
    assert any(key == "autocapture._slug" and op == "call:re.sub" for key, _, op in rows)
    grouped = {}
    for key, line, operation in rows:
        grouped.setdefault(key, []).append({"line": line, "operation": operation})
    print(json.dumps({"controls": "PASS: alias, cross-function, slice, negative, real slug",
                      "files": len(sources), "bytes": sum(len(s.encode()) for s in sources.values()),
                      "operations": len(rows), "functions": grouped}, indent=2))


if __name__ == "__main__":
    main()

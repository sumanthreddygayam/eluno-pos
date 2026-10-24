"""The boundary is enforced, not just drawn: replacing Shopify must not touch domain/ or the use cases."""
import ast
import os
import unittest

from eluno_pos.config import ROOT

PKG = os.path.join(ROOT, "eluno_pos")
STDLIB_OK = {"__future__", "dataclasses", "datetime", "decimal", "json", "re", "typing", "functools", "html"}


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def imports(path):
    tree = ast.parse(read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield 0, a.name
        elif isinstance(node, ast.ImportFrom):
            yield node.level, node.module or ""


def files(sub):
    d = os.path.join(PKG, sub)
    return [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py")]


class Boundaries(unittest.TestCase):
    def test_domain_is_pure(self):
        for path in files("domain"):
            for level, mod in imports(path):
                if level:  # relative: must stay inside domain
                    self.assertEqual(level, 1, "%s reaches outside domain/: %s" % (os.path.basename(path), mod))
                else:
                    self.assertIn(mod.split(".")[0], STDLIB_OK, "%s imports %s (no I/O or platform types in the domain)" % (os.path.basename(path), mod))

    def test_use_cases_talk_to_ports_not_platform_adapters(self):
        for path in files("app"):
            if os.path.basename(path) == "__init__.py":
                continue  # the composition root is the one place that names concrete adapters
            for level, mod in imports(path):
                for banned in ("adapters.shopify", "adapters.smt", "adapters.terminal", "adapters.http"):
                    self.assertNotIn(banned, mod, "%s imports %s" % (os.path.basename(path), mod))

    def test_no_shopify_shapes_outside_the_adapter(self):
        for sub in ("domain", "app"):
            for path in files(sub):
                src = read(path)
                for shape in ("note_attributes", "line_items", "tax_lines", "discount_allocations", "/admin/api/"):
                    self.assertNotIn(shape, src, "%s knows Shopify's %r" % (os.path.relpath(path, PKG), shape))


if __name__ == "__main__":
    unittest.main()

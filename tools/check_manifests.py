#!/usr/bin/env python3
"""Validate every manifest the way Odoo actually reads it.

Odoo parses ``__manifest__.py`` with ``ast.literal_eval``: it must be a bare
dict literal, with no imports and no function calls. An earlier version of this
script used ``exec``, and so happily accepted a manifest that Odoo rejected
outright. It checks the real rules now:

* the file is a pure literal
* every data file it names exists
* the version is one that series of Odoo will accept

    python tools/check_manifests.py
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# (manifest, the Odoo series it is built for)
MANIFESTS = [
    (ROOT / "ibq_whatsapp_helpdesk" / "__manifest__.py", 17),
    (ROOT / "demo" / "addons" / "helpdesk" / "__manifest__.py", 17),
    (ROOT / "dist" / "18.0" / "ibq_whatsapp_helpdesk" / "__manifest__.py", 18),
    (ROOT / "dist" / "18.0" / "helpdesk" / "__manifest__.py", 18),
]


def version_ok(version, series):
    """Odoo 18 accepts x.y, x.y.z, or its own series prefix. 17 is laxer."""
    return bool(re.match(r"^(\d+\.\d+(\.\d+)?|%d\.0\.[\d.]+)$" % series, version))


def main():
    problems = []
    for manifest_path, series in MANIFESTS:
        if not manifest_path.exists():
            problems.append("missing manifest: %s" % manifest_path.relative_to(ROOT))
            continue

        module_dir = manifest_path.parent
        try:
            manifest = ast.literal_eval(manifest_path.read_text(encoding="utf-8"))
        except (ValueError, SyntaxError) as exc:
            problems.append(
                "%s: not a bare literal, Odoo will refuse it (%s)"
                % (module_dir.name, exc)
            )
            continue

        version = manifest.get("version", "")
        if not version_ok(version, series):
            problems.append(
                "%s v%s: version %r is not valid for that series"
                % (module_dir.name, series, version)
            )

        data = manifest.get("data", [])
        for path in [p for p in data if not (module_dir / p).exists()]:
            problems.append(
                "%s v%s: missing data file %s" % (module_dir.name, series, path)
            )

        print("  %-22s v%s  version %-12s %2s data file(s)"
              % (module_dir.name, series, version, len(data)))

    if problems:
        print("\nProblems:")
        for problem in problems:
            print("  " + problem)
        return 1
    print("\nAll manifests are literals Odoo will accept.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

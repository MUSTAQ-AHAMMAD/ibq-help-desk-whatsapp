#!/usr/bin/env python3
"""Evaluate each manifest as Odoo 17 and as Odoo 18, and check every path.

The manifests pick their view files from ``odoo.release.version_info``. That is
a small amount of logic in the one file whose failure mode is "the module does
not install at all", so it is worth checking without booting Odoo: this stubs
``odoo.release``, evaluates the manifest under both versions, and asserts that
every data file it names actually exists.

    python tools/check_manifests.py
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFESTS = [
    ROOT / "ibq_whatsapp_helpdesk" / "__manifest__.py",
    ROOT / "demo" / "addons" / "helpdesk" / "__manifest__.py",
]


def evaluate(manifest_path, version):
    """Run a manifest with a faked odoo.release, returning the dict."""
    odoo = types.ModuleType("odoo")
    release = types.ModuleType("odoo.release")
    release.version_info = (version, 0, 0, "final", 0, "")
    release.version = "%s.0" % version
    odoo.release = release

    saved = {name: sys.modules.get(name) for name in ("odoo", "odoo.release")}
    sys.modules["odoo"] = odoo
    sys.modules["odoo.release"] = release
    try:
        namespace = {"__file__": str(manifest_path)}
        source = manifest_path.read_text(encoding="utf-8")
        # A manifest is a bare dict literal after its imports; capture it by
        # assigning the trailing expression.
        brace = source.index("{")
        exec(source[:brace], namespace)          # noqa: S102 - our own file
        manifest = eval(source[brace:], namespace)  # noqa: S307 - our own file
        return manifest
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def main():
    problems = []
    for manifest_path in MANIFESTS:
        module_dir = manifest_path.parent
        for version in (17, 18):
            manifest = evaluate(manifest_path, version)
            data = manifest.get("data", [])
            if not data:
                problems.append("%s v%s: no data files" % (module_dir.name, version))
                continue

            missing = [p for p in data if not (module_dir / p).exists()]
            for path in missing:
                problems.append("%s v%s: missing %s" % (module_dir.name, version, path))

            # The whole point of the switch: 18 must not load a v17 view file.
            views = [p for p in data if p.split("/")[0] in ("views", "wizard", "v18")]
            if version == 18:
                wrong = [p for p in views if not p.startswith("v18/")]
                for path in wrong:
                    problems.append(
                        "%s v18: still points at the Odoo 17 file %s" % (module_dir.name, path)
                    )
            else:
                wrong = [p for p in views if p.startswith("v18/")]
                for path in wrong:
                    problems.append(
                        "%s v17: wrongly points at %s" % (module_dir.name, path)
                    )

            print("  %-24s v%s  %2s data file(s), all present"
                  % (module_dir.name, version, len(data)))

    if problems:
        print("\nProblems:")
        for problem in problems:
            print("  " + problem)
        return 1
    print("\nBoth manifests resolve cleanly on Odoo 17 and 18.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Generate the Odoo 18 build of the addons from the Odoo 17 sources.

Odoo 17 and 18 cannot be served by one folder, and this is not a style
preference -- it was measured:

* ``__manifest__.py`` is read with ``ast.literal_eval``, so it must be a bare
  literal. It cannot branch on the running version.
* Odoo 18 *rejects* ``<tree>`` with "Invalid view type", and Odoo 17 does not
  know ``<list>``. There is no spelling that satisfies both.
* Odoo 18 validates the manifest ``version``: it must be ``x.y[.z]`` or carry
  the matching ``18.0`` prefix.
* ``ir.cron`` lost ``numbercall`` and ``doall`` in 18; a cron record carrying
  them fails to load.

So the checked-in module is the Odoo 17 build, and this script produces the
Odoo 18 build beside it. Deploy whichever matches your server.

    python tools/build_v18.py            # write dist/18.0/
    python tools/build_v18.py --check    # fail if dist/18.0/ is stale (CI)
"""
import argparse
import filecmp
import pathlib
import re
import shutil
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist" / "18.0"

# (source module, subdirectories whose XML holds views)
MODULES = [
    (ROOT / "ibq_whatsapp_helpdesk", {"views", "wizard"}),
    (ROOT / "demo" / "addons" / "helpdesk", {"views"}),
]

SKIP_DIRS = {"__pycache__", ".git", "v18"}
BANNER = (
    "<!-- GENERATED FOR ODOO 18 - DO NOT EDIT.\n"
    "     Source: {source}\n"
    "     Rebuild with tools/build_v18.py after editing the Odoo 17 file. -->\n"
)


def port_view(xml, source_name):
    """Rewrite one Odoo 17 view file into its Odoo 18 spelling."""
    # 1. The list view was renamed, and 18 rejects the old tag outright.
    xml = re.sub(r"<tree(\s|>)", r"<list\1", xml)
    xml = xml.replace("</tree>", "</list>")

    # 2. Window actions name view types, and "tree" is no longer one.
    def view_mode(match):
        modes = ["list" if m.strip() == "tree" else m.strip()
                 for m in match.group(1).split(",")]
        return '<field name="view_mode">%s</field>' % ",".join(modes)

    xml = re.sub(r'<field name="view_mode">([^<]+)</field>', view_mode, xml)
    xml = re.sub(r'(<field name="binding_view_types">)tree(</field>)',
                 r"\1list\2", xml)

    # 3. The kanban card template was renamed.
    xml = xml.replace('t-name="kanban-box"', 't-name="card"')

    # 4. The chatter is a real element in 18, not a div of magic classes.
    xml = re.sub(r'<div class="oe_chatter">.*?</div>', "<chatter/>", xml,
                 flags=re.DOTALL)

    # Banner after the XML declaration, which must stay first in the file.
    banner = BANNER.format(source=source_name)
    match = re.match(r"(<\?xml[^>]*\?>\s*)", xml)
    return (match.group(1) + banner + xml[match.end():]) if match else banner + xml


def port_data(xml, source_name):
    """Rewrite non-view data records for Odoo 18.

    ``ir.cron`` dropped ``numbercall`` and ``doall`` -- scheduled actions now
    always repeat -- and a record still setting them is rejected outright.
    """
    for gone in ("numbercall", "doall"):
        xml = re.sub(
            r'[ \t]*<field name="%s"[^>]*(?:/>|>[^<]*</field>)[ \t]*\r?\n?' % gone,
            "", xml,
        )

    banner = BANNER.format(source=source_name)
    match = re.match(r"(<\?xml[^>]*\?>\s*)", xml)
    return (match.group(1) + banner + xml[match.end():]) if match else banner + xml


def port_manifest(text):
    """Re-series the version so Odoo 18 accepts it."""
    return re.sub(r'"version":\s*"(?:17\.0\.)?([\d.]+)"',
                  lambda m: '"version": "18.0.%s"' % m.group(1), text, count=1)


def build_module(module, view_dirs, out_root, staged):
    """Copy one module into out_root, porting what needs porting."""
    for source in sorted(module.rglob("*")):
        if any(part in SKIP_DIRS for part in source.relative_to(module).parts):
            continue
        if not source.is_file():
            continue

        relative = source.relative_to(module)
        target = out_root / module.name / relative
        target.parent.mkdir(parents=True, exist_ok=True)

        text_parts = relative.parts
        if relative.name == "__manifest__.py":
            target.write_text(
                port_manifest(source.read_text(encoding="utf-8")), encoding="utf-8"
            )
        elif relative.suffix == ".xml" and text_parts[0] in view_dirs:
            target.write_text(
                port_view(source.read_text(encoding="utf-8"), str(relative)),
                encoding="utf-8",
            )
        elif relative.suffix == ".xml" and text_parts[0] == "data":
            target.write_text(
                port_data(source.read_text(encoding="utf-8"), str(relative)),
                encoding="utf-8",
            )
        else:
            shutil.copy2(source, target)
        staged.append(target)


def build(check_only=False):
    scratch = DIST.with_name("18.0.tmp") if check_only else DIST
    if scratch.exists():
        shutil.rmtree(scratch)

    staged = []
    for module, view_dirs in MODULES:
        if module.exists():
            build_module(module, view_dirs, scratch, staged)

    if not check_only:
        print("Wrote %s file(s) to %s" % (len(staged), DIST.relative_to(ROOT)))
        return 0

    # Compare the freshly generated tree against what is committed.
    differences = []
    if not DIST.exists():
        differences.append("dist/18.0 is missing entirely")
    else:
        fresh = {p.relative_to(scratch) for p in scratch.rglob("*") if p.is_file()}
        committed = {p.relative_to(DIST) for p in DIST.rglob("*") if p.is_file()}
        for path in sorted(fresh - committed):
            differences.append("missing: %s" % path)
        for path in sorted(committed - fresh):
            differences.append("stale extra: %s" % path)
        for path in sorted(fresh & committed):
            if not filecmp.cmp(scratch / path, DIST / path, shallow=False):
                differences.append("differs: %s" % path)
    shutil.rmtree(scratch)

    if differences:
        print("dist/18.0 is out of date; re-run tools/build_v18.py:")
        for line in differences[:20]:
            print("  " + line)
        return 1
    print("dist/18.0 matches the Odoo 17 sources.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed Odoo 18 build is stale")
    sys.exit(build(parser.parse_args().check))

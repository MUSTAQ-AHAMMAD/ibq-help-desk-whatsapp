#!/usr/bin/env python3
"""Generate the Odoo 18 view files from the Odoo 17 sources.

Odoo renamed several view constructs in 18, and the old spellings either warn
or fail outright. XML cannot branch on a version the way Python can, so the
17 files stay canonical and this produces an 18 variant beside them:

    ibq_whatsapp_helpdesk/views/*.xml   ->  ibq_whatsapp_helpdesk/v18/views/*.xml
    ibq_whatsapp_helpdesk/wizard/*.xml  ->  ibq_whatsapp_helpdesk/v18/wizard/*.xml

``__manifest__.py`` picks the right set at load time from ``odoo.release``, so
the same folder installs on either version. Edit the 17 files and re-run this;
never hand-edit anything under ``v18/``.

    python tools/build_v18.py [--check]

``--check`` regenerates into memory and exits non-zero if the committed output
is stale, which is what CI should run.
"""
import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Every module whose views need porting, and the subdirectories that hold them.
TARGETS = [
    (ROOT / "ibq_whatsapp_helpdesk", ["views", "wizard"]),
    (ROOT / "demo" / "addons" / "helpdesk", ["views"]),
]

BANNER = (
    "<!-- GENERATED FILE - DO NOT EDIT.\n"
    "     Built from ../../{source} by tools/build_v18.py for Odoo 18.\n"
    "     Edit the Odoo 17 source and re-run the script. -->\n"
)


def port(xml, source_name):
    """Rewrite one Odoo 17 view file into its Odoo 18 spelling."""
    original = xml

    # 1. The list view was renamed. <tree> is deprecated in 18 and gone after.
    xml = re.sub(r"<tree(\s|>)", r"<list\1", xml)
    xml = xml.replace("</tree>", "</list>")

    # 2. Window actions name view types, and "tree" is no longer one of them.
    def fix_view_mode(match):
        modes = [m.strip() for m in match.group(1).split(",")]
        modes = ["list" if m == "tree" else m for m in modes]
        return "<field name=\"view_mode\">%s</field>" % ",".join(modes)

    xml = re.sub(
        r'<field name="view_mode">([^<]+)</field>', fix_view_mode, xml
    )

    # 3. binding_view_types uses the same vocabulary.
    xml = re.sub(
        r'(<field name="binding_view_types">)list(</field>)', r"\1list\2", xml
    )
    xml = re.sub(
        r'(<field name="binding_view_types">)tree(</field>)', r"\1list\2", xml
    )

    # 4. The kanban card template was renamed, and the wrapper is implicit.
    xml = xml.replace('t-name="kanban-box"', 't-name="card"')

    # 5. The chatter is a real element in 18, not a div of magic classes.
    xml = re.sub(
        r'<div class="oe_chatter">.*?</div>',
        "<chatter/>",
        xml,
        flags=re.DOTALL,
    )

    if xml == original:
        return None  # nothing to port; the file is already version-neutral

    # The banner goes *after* the XML declaration -- that has to be the very
    # first thing in the file or no parser will touch it.
    banner = BANNER.format(source=source_name)
    match = re.match(r"(<\?xml[^>]*\?>\s*)", xml)
    if match:
        return match.group(1) + banner + xml[match.end():]
    return banner + xml


def build(check_only=False):
    stale, written, skipped = [], 0, 0

    for module, subdirs in TARGETS:
        if not module.exists():
            continue
        out_root = module / "v18"
        for subdir in subdirs:
            source_dir = module / subdir
            if not source_dir.exists():
                continue
            for source in sorted(source_dir.glob("*.xml")):
                relative = "%s/%s" % (subdir, source.name)
                ported = port(source.read_text(encoding="utf-8"), relative)
                target = out_root / subdir / source.name

                if ported is None:
                    # Version-neutral: copy verbatim so the manifest can point
                    # every path at v18/ without special cases.
                    ported = source.read_text(encoding="utf-8")
                    skipped += 1

                if check_only:
                    current = target.read_text(encoding="utf-8") if target.exists() else None
                    if current != ported:
                        stale.append(str(target.relative_to(ROOT)))
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(ported, encoding="utf-8")
                written += 1

    if check_only:
        if stale:
            print("Stale Odoo 18 output, re-run tools/build_v18.py:")
            for path in stale:
                print("  " + path)
            return 1
        print("Odoo 18 output is up to date.")
        return 0

    print("Wrote %s file(s) (%s needed no changes)." % (written, skipped))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed v18 output is stale")
    sys.exit(build(parser.parse_args().check))

#!/usr/bin/env python3
"""Assemble the shippable release folders and zip them.

Produces one self-contained package per Odoo series:

    dist/release/ibq_whatsapp_helpdesk-17.0/   + .zip
    dist/release/ibq_whatsapp_helpdesk-18.0/   + .zip

Each carries the module, the install guide, the licence, the Python
requirements, and -- separately, so it cannot be picked up by accident -- the
Community helpdesk stub for sites without Odoo Enterprise.

    python tools/build_release.py
"""
import pathlib
import shutil
import sys
import zipfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import build_v18  # noqa: E402  - sibling tool, regenerates the Odoo 18 build

ROOT = pathlib.Path(__file__).resolve().parent.parent
RELEASE = ROOT / "dist" / "release"

# series -> (module source, community stub source)
SOURCES = {
    "17.0": (ROOT / "ibq_whatsapp_helpdesk", ROOT / "demo" / "addons" / "helpdesk"),
    "18.0": (ROOT / "dist" / "18.0" / "ibq_whatsapp_helpdesk",
             ROOT / "dist" / "18.0" / "helpdesk"),
}

JUNK = {"__pycache__", ".git", ".pytest_cache", ".DS_Store", "Thumbs.db"}

REQUIREMENTS = """\
# Python packages this addon needs beyond a stock Odoo install.
# Install into the same interpreter/virtualenv that runs Odoo:
#     pip install -r requirements.txt
requests>=2.25.0
"""

LICENSE = """\
IBQ WhatsApp Helpdesk (Twilio)
Licensed under the GNU Lesser General Public License v3.0 (LGPL-3).

You may use, modify and redistribute this addon under the terms of that
licence. The full text is at https://www.gnu.org/licenses/lgpl-3.0.txt -- if
you redistribute this package, include a copy of it alongside these files.

This addon talks to Twilio's Programmable Messaging API. Twilio and WhatsApp
are trademarks of their respective owners and are not affiliated with this
project.
"""


def copy_clean(source, target):
    """Copy a module directory, dropping build artefacts."""
    shutil.copytree(
        source, target,
        ignore=lambda directory, names: [n for n in names if n in JUNK
                                         or n.endswith((".pyc", ".pyo"))],
    )


def build(series):
    module_source, stub_source = SOURCES[series]
    name = "ibq_whatsapp_helpdesk-%s" % series
    package = RELEASE / name
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)

    # The addon itself, in the folder the addons_path should point at.
    copy_clean(module_source, package / "addons" / "ibq_whatsapp_helpdesk")

    # The Community stub lives outside addons/ deliberately: pointing Odoo at
    # addons/ must never pick it up by accident on an Enterprise site.
    copy_clean(stub_source, package / "optional-community-helpdesk-stub" / "helpdesk")

    (package / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (package / "LICENSE.txt").write_text(LICENSE, encoding="utf-8")
    shutil.copy2(ROOT / "README.md", package / "README.md")

    # The diagnostic script the install guide points at.
    tools_dir = package / "tools"
    tools_dir.mkdir()
    shutil.copy2(ROOT / "tools" / "find-missing-mail-dep.sh",
                 tools_dir / "find-missing-mail-dep.sh")

    install_doc = ROOT / "tools" / "install_template.md"
    text = install_doc.read_text(encoding="utf-8")
    (package / "INSTALL.md").write_text(
        text.replace("{{SERIES}}", series).replace("{{NAME}}", name),
        encoding="utf-8",
    )

    archive = RELEASE / ("%s.zip" % name)
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(package.rglob("*")):
            if path.is_file():
                zipped.write(path, path.relative_to(RELEASE))

    return package, archive


def verify(package, archive, series):
    """Refuse to ship a package that is missing something obvious."""
    problems = []
    module = package / "addons" / "ibq_whatsapp_helpdesk"

    for required in ("__manifest__.py", "__init__.py", "models/__init__.py",
                     "security/ir.model.access.csv", "controllers/__init__.py",
                     "static/src/dashboard/whatsapp_dashboard.xml"):
        if not (module / required).exists():
            problems.append("module is missing %s" % required)

    import ast
    manifest = ast.literal_eval((module / "__manifest__.py").read_text(encoding="utf-8"))
    if not manifest["version"].startswith(series):
        problems.append("manifest version %r is not %s"
                        % (manifest["version"], series))
    for data_file in manifest["data"]:
        if not (module / data_file).exists():
            problems.append("manifest lists a missing data file: %s" % data_file)

    # Every Python module referenced by an __init__ must be present.
    for init in module.rglob("__init__.py"):
        for line in init.read_text(encoding="utf-8").splitlines():
            if line.startswith("from . import "):
                child = line.split()[-1]
                if not ((init.parent / (child + ".py")).exists()
                        or (init.parent / child).is_dir()):
                    problems.append("%s imports missing %s" % (init.name, child))

    for junk in module.rglob("__pycache__"):
        problems.append("build artefact shipped: %s" % junk)

    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            problems.append("zip is corrupt")
        names = zipped.namelist()
        for expected in ("INSTALL.md", "README.md", "LICENSE.txt",
                         "requirements.txt", "find-missing-mail-dep.sh"):
            if not any(n.endswith("/" + expected) for n in names):
                problems.append("zip is missing %s" % expected)

    return problems


def main():
    build_v18.build()  # keep the Odoo 18 sources current before packaging
    RELEASE.mkdir(parents=True, exist_ok=True)

    failed = False
    for series in sorted(SOURCES):
        package, archive = build(series)
        problems = verify(package, archive, series)
        size = archive.stat().st_size / 1024
        files = sum(1 for p in package.rglob("*") if p.is_file())
        print("%s  %3s files  %6.0f KB  %s"
              % (archive.name, files, size, "OK" if not problems else "PROBLEMS"))
        for problem in problems:
            print("    - " + problem)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

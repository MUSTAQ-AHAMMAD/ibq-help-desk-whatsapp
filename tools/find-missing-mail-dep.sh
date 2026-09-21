#!/bin/bash
# Lists custom modules that extend a mail.* model and flags any that forget to
# declare 'mail' in their manifest depends. Read-only; safe on production.
#
#   cd /home/odoo/src/user     # or wherever your custom addons live
#   bash find-missing-mail-dep.sh

for module in */ ; do
    manifest="$module/__manifest__.py"
    [ -f "$manifest" ] || continue

    extends=$(grep -rhoE "_inherit[^=]*=[^\"']*[\"']mail\.[a-z._]+[\"']" "$module" 2>/dev/null \
              | grep -oE "mail\.[a-z._]+" | sort -u | paste -sd' ')
    [ -z "$extends" ] && continue

    depends=$(python3 - "$manifest" <<'PY'
import ast, sys
print(",".join(ast.literal_eval(open(sys.argv[1]).read()).get("depends", [])))
PY
)
    case ",$depends," in
        *,mail,*) verdict="ok" ;;
        *)        verdict="<<< MISSING mail -- THIS IS THE CULPRIT" ;;
    esac

    printf '%-32s extends: %-32s depends: %-26s %s\n' \
           "${module%/}" "$extends" "$depends" "$verdict"
done

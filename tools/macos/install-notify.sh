#!/bin/sh

set -eu

url=http://localhost:8765
label=com.example.keepsake-notify
password_file=
launchctl_bin=${KEEPSAKE_NOTIFY_LAUNCHCTL:-launchctl}

usage() {
    echo "usage: install-notify.sh --password-file FILE [--url URL]" >&2
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --password-file)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            password_file=$2
            shift 2
            ;;
        --url)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            url=$2
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done

[ -n "$password_file" ] || { echo "a password file is required" >&2; exit 2; }
[ -f "$password_file" ] || { echo "the password file does not exist" >&2; exit 2; }
if ! python3 -c 'import os, sys
sys.exit(0 if os.stat(sys.argv[1]).st_mode & 0o077 == 0 else 1)' "$password_file"
then
    echo "the password file must not be readable or writable by group or others" >&2
    exit 2
fi

case $password_file in
    /*) ;;
    *) password_file=$(cd "$(dirname "$password_file")" && pwd -P)/$(basename "$password_file") ;;
esac

here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
install_dir="$HOME/Library/Application Support/keepsake-notify"
agents_dir=$HOME/Library/LaunchAgents
script_path=$install_dir/keepsake-notify.sh
plist_path=$agents_dir/$label.plist
plist_tmp=$agents_dir/.$label.$$.plist
domain=gui/$(id -u)

umask 077
mkdir -p "$install_dir" "$agents_dir"
if [ -f "$plist_path" ]; then
    "$launchctl_bin" bootout "$domain" "$plist_path" >/dev/null 2>&1 || :
fi
install -m 700 "$here/keepsake-notify.sh" "$script_path"
python3 -c 'import re, sys
from xml.sax.saxutils import escape
text = open(sys.argv[1], encoding="utf-8").read()
keys = ("__SCRIPT_PATH__", "__PASSWORD_FILE__", "__URL__", "__LABEL__", "__HOME__")
values = dict(zip(keys, sys.argv[3:]))
text = re.sub("|".join(keys), lambda m: escape(values[m.group(0)]), text)
open(sys.argv[2], "w", encoding="utf-8").write(text)' \
    "$here/com.example.keepsake-notify.plist.template" "$plist_tmp" \
    "$script_path" "$password_file" "$url" "$label" "$HOME"
chmod 600 "$plist_tmp"
mv "$plist_tmp" "$plist_path"
"$launchctl_bin" bootstrap "$domain" "$plist_path"
echo "installed $label"

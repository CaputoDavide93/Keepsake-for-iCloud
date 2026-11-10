#!/bin/sh

set -eu

label=com.example.keepsake-notify
launchctl_bin=${KEEPSAKE_NOTIFY_LAUNCHCTL:-launchctl}

usage() {
    echo "usage: uninstall-notify.sh" >&2
}

while [ "$#" -gt 0 ]; do
    case $1 in
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


install_dir="$HOME/Library/Application Support/keepsake-notify"
plist_path=$HOME/Library/LaunchAgents/$label.plist
domain=gui/$(id -u)

if [ -f "$plist_path" ]; then
    "$launchctl_bin" bootout "$domain" "$plist_path" >/dev/null 2>&1 || :
fi
rm -f "$plist_path" "$install_dir/keepsake-notify.sh"
echo "uninstalled $label"

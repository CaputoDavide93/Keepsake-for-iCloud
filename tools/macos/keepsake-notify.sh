#!/bin/sh

set -u

url=${KEEPSAKE_NOTIFY_URL:-http://localhost:8765}
password_file=${KEEPSAKE_NOTIFY_PASSWORD_FILE:-}
state_dir=${KEEPSAKE_NOTIFY_STATE_DIR:-${HOME:?HOME is not set}/Library/Application Support/keepsake-notify}
once=false

usage() {
    echo "usage: keepsake-notify.sh [--once] [--url URL] [--password-file FILE]" >&2
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --once)
            once=true
            shift
            ;;
        --url)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            url=$2
            shift 2
            ;;
        --password-file)
            [ "$#" -ge 2 ] || { usage; exit 2; }
            password_file=$2
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
[ -f "$password_file" ] && [ -r "$password_file" ] && [ -s "$password_file" ] || {
    echo "the password file is missing, empty, or unreadable" >&2
    exit 2
}
command -v curl >/dev/null 2>&1 || { echo "curl is required" >&2; exit 2; }
command -v osascript >/dev/null 2>&1 || { echo "osascript is required" >&2; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 2; }

if ! python3 -c 'import sys
p = open(sys.argv[1], encoding="utf-8").read().rstrip("\r\n")
sys.exit(0 if p and "\n" not in p and "\r" not in p else 1)' "$password_file"
then
    echo "the password file must contain one non-empty line" >&2
    exit 2
fi

umask 077
mkdir -p "$state_dir" || exit 2
chmod 700 "$state_dir" 2>/dev/null || :
body_file=$state_dir/.status.$$
current_file=$state_dir/.attention.$$
notice_file=$state_dir/.notice.$$
trap 'rm -f "$body_file" "$current_file" "$notice_file"' EXIT HUP INT TERM

state_value() {
    if [ -f "$state_dir/$1" ]; then
        sed -n '1p' "$state_dir/$1"
    else
        printf '%s' unknown
    fi
}

set_state() {
    printf '%s\n' "$2" > "$state_dir/$1"
}

notify() {
    # The fixed AppleScript reads title and message as data. API text is never
    # interpolated into source code, so quotes and backslashes stay harmless.
    printf '%s\n%s\n' "$1" "$2" > "$notice_file"
    osascript - "$notice_file" >/dev/null 2>&1 <<'APPLESCRIPT' || :
on run argv
    set noticeText to read POSIX file (item 1 of argv) as «class utf8»
    set noticeLines to paragraphs of noticeText
    display notification (item 2 of noticeLines) with title (item 1 of noticeLines)
end run
APPLESCRIPT
    rm -f "$notice_file"
}

mark_reachable() {
    if [ "$(state_value reachable)" = no ]; then
        notify "Keepsake" "The Keepsake API is reachable again."
    fi
    set_state reachable yes
}

poll() {
    endpoint=${url%/}/api/status
    if http_code=$(
        python3 -c 'import json, sys
p = open(sys.argv[1], encoding="utf-8").read().rstrip("\r\n")
print("header = " + json.dumps("Authorization: Bearer " + p, ensure_ascii=False))' \
            "$password_file" |
            curl --silent --show-error --connect-timeout 10 --max-time 30 \
                --output "$body_file" --write-out '%{http_code}' --config - -- "$endpoint" \
                2>/dev/null
    ); then
        mark_reachable
    else
        if [ "$(state_value reachable)" != no ]; then
            notify "Keepsake" "The Keepsake API is unreachable."
        fi
        set_state reachable no
        return 1
    fi

    case $http_code in
        401|403)
            if [ "$(state_value auth)" != rejected ]; then
                notify "Keepsake" "The Keepsake notification password was rejected."
            fi
            set_state auth rejected
            return 1
            ;;
        200)
            set_state auth accepted
            ;;
        *)
            return 1
            ;;
    esac

    if ! python3 -c 'import json, re, sys
d = json.load(open(sys.argv[1], encoding="utf-8"))
a = d["attention"]
assert isinstance(a, list) and d["attention_count"] == len(a)
seen = set()
for item in a:
    job, reason = item["job"], item["reason"]
    assert isinstance(job, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", job)
    assert isinstance(reason, str)
    if job not in seen:
        print(job + "\t" + " ".join(reason.split()))
        seen.add(job)' "$body_file" > "$current_file" 2>/dev/null
    then
        return 1
    fi

    old_file=$state_dir/attention
    [ -f "$old_file" ] || : > "$old_file"
    tab=$(printf '\t')
    while IFS="$tab" read -r job reason; do
        [ -n "$job" ] || continue
        if ! grep -Fqx "$job" "$old_file"; then
            notify "Keepsake" "$job needs attention: $reason"
        fi
    done < "$current_file"

    while IFS= read -r job; do
        [ -n "$job" ] || continue
        if ! cut -f 1 "$current_file" | grep -Fqx "$job"; then
            notify "Keepsake" "$job no longer needs attention."
        fi
    done < "$old_file"

    cut -f 1 "$current_file" > "$old_file"
    return 0
}

while :; do
    result=0
    poll || result=$?
    $once && exit "$result"
    sleep 60
done

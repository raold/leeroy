#!/usr/bin/env bash
# Key-only config reports. Sourced after common.sh; never prints an assignment's value.

# Twenty additions AND removals distinguish a broad prefix rename from ordinary
# option churn. This is a warning heuristic, never an automatic migration.
AC_CONFIG_RENAME_THRESHOLD=20

# Active assignments by default; with "all", commented ones ("# Key = 1") count too, so a key
# the owner commented out on purpose, or a template default left commented, is not reported.
# A leading UTF-8 BOM (Windows editors) is stripped so the first key is read.
ac_config_keys() {
    awk -v all="${2:-}" 'NR == 1 { sub(/^\357\273\277/, "") }
        (all == "all" && /^[[:space:]]*#?[[:space:]]*[A-Za-z][A-Za-z0-9_.]*[[:space:]]*=/) ||
        /^[[:space:]]*[A-Za-z][A-Za-z0-9_.]*[[:space:]]*=/ {
            sub(/^[[:space:]]*#?[[:space:]]*/, ""); sub(/[[:space:]]*=.*/, ""); print
        }' "$1" | LC_ALL=C sort -u
}

ac_config_prefix() {
    awk '{ if (index($0, ".")) sub(/\..*$/, "."); else $0 = "(no dotted prefix)"; print }' |
        LC_ALL=C sort | uniq -c | LC_ALL=C sort -k1,1nr -k2,2 |
        awk 'NR == 1 { sub(/^[[:space:]]*[0-9]+[[:space:]]+/, ""); print }'
}

ac_report_config_keys() {
    local dist="$1" live="$2" file key old_prefix new_prefix dist_keys live_keys dist_all live_all
    local -a added=() gone=()
    [ -f "$dist" ] || return 0
    file="$(basename "$live")"
    if [ ! -f "$live" ]; then
        warn "$file missing from $(dirname "$live") -- copy it from $dist"
        return 0
    fi
    # Capture read failures before process substitution, whose exit status mapfile
    # cannot see. An unreadable file must not masquerade as an empty configuration.
    dist_keys=$(ac_config_keys "$dist") || { warn "cannot read template keys: $file"; return 1; }
    live_keys=$(ac_config_keys "$live") || { warn "cannot read config keys: $file"; return 1; }
    dist_all=$(ac_config_keys "$dist" all) || { warn "cannot read template keys: $file"; return 1; }
    live_all=$(ac_config_keys "$live" all) || { warn "cannot read config keys: $file"; return 1; }
    # New: active in the template, absent from the live file even as a comment.
    # Gone: active in the live file, absent from the template even as a comment.
    mapfile -t added < <(LC_ALL=C comm -23 <(printf '%s' "$dist_keys") <(printf '%s' "$live_all"))
    mapfile -t gone < <(LC_ALL=C comm -13 <(printf '%s' "$dist_all") <(printf '%s' "$live_keys"))
    if [ "${#added[@]}" -gt 0 ]; then
        log "new config keys: $file"
        for key in "${added[@]}"; do printf '    new key: %-50s (%s)\n' "$key" "$file"; done
    fi
    if [ "${#gone[@]}" -gt 0 ]; then
        log "config keys no longer in the template: $file"
        for key in "${gone[@]}"; do printf '    gone key: %-49s (%s)\n' "$key" "$file"; done
    fi
    if [ "${#added[@]}" -ge "$AC_CONFIG_RENAME_THRESHOLD" ] && [ "${#gone[@]}" -ge "$AC_CONFIG_RENAME_THRESHOLD" ]; then
        old_prefix=$(printf '%s\n' "${gone[@]}" | ac_config_prefix)
        new_prefix=$(printf '%s\n' "${added[@]}" | ac_config_prefix)
        warn "suspect a rename in $file: $old_prefix -> $new_prefix"
    fi
}

# Never fails: the update has already pulled by now, so one unreadable file is reported
# and the rest are still checked (common.sh's errexit would otherwise stop the update here).
ac_report_all_config_keys() {
    local repo dist app incomplete=0
    log "checking new and removed config keys"
    for app in worldserver authserver; do
        ac_report_config_keys "$AC_SRC/src/server/apps/$app/$app.conf.dist" "$AC_CONF/$app.conf" || incomplete=1
    done
    for repo in "$@"; do
        for dist in "$repo"/conf/*.conf.dist; do
            ac_report_config_keys "$dist" "$AC_MODCONF/$(basename "$dist" .dist)" || incomplete=1
        done
    done
    [ "$incomplete" = 0 ] || warn "config key report incomplete: see the lines above"
    return 0
}

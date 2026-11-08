#!/usr/bin/env bash
# Key-only config reports. Sourced after common.sh; never prints an assignment's value.

# Twenty additions AND removals distinguish a broad prefix rename from ordinary
# option churn. This is a warning heuristic, never an automatic migration.
AC_CONFIG_RENAME_THRESHOLD=20

ac_config_keys() {
    awk '/^[[:space:]]*[A-Za-z][A-Za-z0-9_.]*[[:space:]]*=/ {
        sub(/^[[:space:]]*/, ""); sub(/[[:space:]]*=.*/, ""); print
    }' "$1" | LC_ALL=C sort -u
}

ac_config_prefix() {
    awk '{ if (index($0, ".")) sub(/\..*$/, "."); else $0 = "(no dotted prefix)"; print }' |
        LC_ALL=C sort | uniq -c | LC_ALL=C sort -k1,1nr -k2,2 |
        awk 'NR == 1 { sub(/^[[:space:]]*[0-9]+[[:space:]]+/, ""); print }'
}

ac_report_config_keys() {
    local dist="$1" live="$2" file key old_prefix new_prefix
    local -a added=() gone=()
    [ -f "$dist" ] || return 0
    file="$(basename "$live")"
    if [ ! -f "$live" ]; then
        warn "$file missing from $(dirname "$live") -- copy it from $dist"
        return 0
    fi
    mapfile -t added < <(LC_ALL=C comm -23 <(ac_config_keys "$dist") <(ac_config_keys "$live"))
    mapfile -t gone < <(LC_ALL=C comm -13 <(ac_config_keys "$dist") <(ac_config_keys "$live"))
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

ac_report_all_config_keys() {
    local repo dist app
    log "checking new and removed config keys"
    for app in worldserver authserver; do
        ac_report_config_keys "$AC_SRC/src/server/apps/$app/$app.conf.dist" "$AC_CONF/$app.conf"
    done
    for repo in "$@"; do
        for dist in "$repo"/conf/*.conf.dist; do
            ac_report_config_keys "$dist" "$AC_MODCONF/$(basename "$dist" .dist)"
        done
    done
}

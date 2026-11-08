#!/usr/bin/env bash
# Only invented configs and a fake git command; never touches the installed server.
set -euo pipefail
scripts=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
temp_root=$(cd "${TMPDIR:-/tmp}" && pwd -P)
scratch=$(mktemp -d "$temp_root/leeroy-config-keys.XXXXXX")
case "$scratch" in "$temp_root"/leeroy-config-keys.*) ;; *) exit 1 ;; esac
trap 'rm -rf -- "$scratch"' EXIT
export AC_ROOT="$scratch/install with spaces"
# shellcheck source=common.sh
source "$scripts/common.sh"
# shellcheck source=config-keys.sh
source "$scripts/config-keys.sh"

checks=0
has() { grep -Fq -- "$1" <<< "$output" || { printf 'FAIL: missing %s\n' "$1" >&2; exit 1; }; checks=$((checks + 1)); }
lacks() { if grep -Fq -- "$1" <<< "$output"; then printf 'FAIL: unexpected %s\n' "$1" >&2; exit 1; fi; checks=$((checks + 1)); }
mkdir -p "$AC_CONF" "$AC_MODCONF" "$AC_SRC/src/server/apps/"{worldserver,authserver} \
    "$AC_MODULES/example/conf" "$AC_MODULES/example/.git"

dist="$AC_MODULES/example/conf/example.conf.dist"
live="$AC_MODCONF/example.conf"
printf 'Shared.Key = 1\n New.Key = 2\nDot.Key = 3\nTest.Password = "VALUE_MUST_STAY_PRIVATE"\n' > "$dist"
printf 'Shared.Key=4\nGone.Key = 5\nDotXKey = 6\n# New.Key = 2\n' > "$live"
output=$(ac_report_config_keys "$dist" "$live" 2>&1)
has 'new config keys: example.conf'
has 'New.Key'
has 'Dot.Key' # Dots compare literally; DotXKey is a different key.
has 'config keys no longer in the template: example.conf'
has 'Gone.Key'
lacks 'Shared.Key'
lacks 'VALUE_MUST_STAY_PRIVATE'
lacks 'suspect a rename'

# Empty/comment-only inputs must succeed under common.sh's errexit/pipefail.
printf '# Disabled.Key = 1\n' > "$dist"
: > "$live"
output=$(ac_report_config_keys "$dist" "$live" 2>&1)
[ -z "$output" ] || exit 1
checks=$((checks + 1))
printf 'New.Password = "DIST_SECRET_SENTINEL"\r\n' > "$dist"
printf 'Old.Password = "LIVE_SECRET_SENTINEL"\r\n' > "$live"
for i in {1..19}; do
    printf 'Playerbots.Key%s = "DIST_SECRET_SENTINEL"\n' "$i" >> "$dist"
    printf 'AiPlayerbot.Key%s = "LIVE_SECRET_SENTINEL"\n' "$i" >> "$live"
done
output=$(ac_report_config_keys "$dist" "$live" 2>&1)
has 'suspect a rename in example.conf: AiPlayerbot. -> Playerbots.'
lacks 'DIST_SECRET_SENTINEL'
lacks 'LIVE_SECRET_SENTINEL'
# Twenty additions alone are not a suspected rename.
printf 'Old.Password = "LIVE_SECRET_SENTINEL"\n' > "$live"
output=$(ac_report_config_keys "$dist" "$live" 2>&1)
lacks 'suspect a rename'
for i in {1..18}; do printf 'AiPlayerbot.Key%s = 0\n' "$i" >> "$live"; done
output=$(ac_report_config_keys "$dist" "$live" 2>&1)
lacks 'suspect a rename' # Twenty new, nineteen gone is still below the threshold.
printf 'AiPlayerbot.Key19 = 0\n' >> "$live"

printf 'World.New = 1\n' > "$AC_SRC/src/server/apps/worldserver/worldserver.conf.dist"
printf 'World.Old = "WORLD_SECRET_SENTINEL"\n' > "$AC_CONF/worldserver.conf"
printf 'Auth.New = 1\n' > "$AC_SRC/src/server/apps/authserver/authserver.conf.dist"
printf 'Auth.Old = "AUTH_SECRET_SENTINEL"\n' > "$AC_CONF/authserver.conf"
output=$(ac_report_all_config_keys "$AC_SRC" "$AC_MODULES/example" 2>&1)
has '(worldserver.conf)'
has '(authserver.conf)'
has '(example.conf)'
has 'World.New'
has 'World.Old'
has 'Auth.New'
has 'Auth.Old'
lacks 'SECRET_SENTINEL'
output=$(ac_report_config_keys "$dist" "$AC_MODCONF/missing.conf" 2>&1)
has 'missing.conf missing from'

# Exercise ac-update.sh itself without any real git or network operations.
# An ahead/behind fixture must fetch but never merge/rebase in --dry-run.
mkdir -p "$scratch/bin" "$scratch/scripts"
cp "$scripts/ac-update.sh" "$scripts/common.sh" "$scripts/config-keys.sh" "$scratch/scripts/"
export GIT_TEST_LOG="$scratch/git.log" GIT_TEST_BEHIND=2
cat > "$scratch/bin/git" <<'GIT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$GIT_TEST_LOG"
[ "$1" = -C ] || exit 80
shift 2
case "$1" in
    status) ;;
    branch) printf 'main\n' ;;
    rev-parse) printf 'origin/main\n' ;;
    fetch) ;;
    rev-list) if [ "$3" = 'HEAD..origin/main' ]; then printf '%s\n' "$GIT_TEST_BEHIND"; else printf '0\n'; fi ;;
    *) exit 81 ;;
esac
GIT
chmod +x "$scratch/bin/git"
output=$(PATH="$scratch/bin:$PATH" bash "$scratch/scripts/ac-update.sh" --dry-run 2>&1)
has 'dry run, nothing changed'
has 'World.New'
has 'Auth.Old'
has 'suspect a rename in example.conf'
lacks 'SECRET_SENTINEL'
[ ! -e "$scratch/scripts/.last-update" ] || exit 1
grep -q ' fetch ' "$GIT_TEST_LOG"
if grep -Eq ' (merge|rebase) ' "$GIT_TEST_LOG"; then exit 1; fi
checks=$((checks + 3))
export GIT_TEST_BEHIND=0
output=$(PATH="$scratch/bin:$PATH" bash "$scratch/scripts/ac-update.sh" --dry-run 2>&1)
has 'everything already current'
has 'Auth.New'
has 'World.Old'

printf 'ac-update-keys: %s checks passed\n' "$checks"

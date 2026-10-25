#!/bin/sh
# AI-assisted local acceptance harness (OpenAI Codex). No account access.
set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 DEX_SOURCE PATCHED_POMERIUM_SOURCE" >&2
    exit 2
fi
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
dex=$(CDPATH= cd -- "$1" && pwd -P)
pomerium=$(CDPATH= cd -- "$2" && pwd -P)
go=${GO_BINARY:-go}
coordinator="$pomerium/pkg/identity/refreshcoord/coordinator.go"
[ -f "$coordinator" ] || { echo "missing patched coordinator source" >&2; exit 1; }
[ "${CGO_ENABLED:-1}" = 1 ] || { echo "CGO_ENABLED=1 is required for SQLite/race coverage" >&2; exit 1; }
export CGO_ENABLED=1
export GOMAXPROCS=${GOMAXPROCS:-2}

work=$(mktemp -d "${TMPDIR:-/tmp}/dex-acceptance.XXXXXX")
testfile="$dex/server/weechat_authority_acceptance_test.go"
created_test=false
cleanup() {
    if [ "$created_test" = true ]; then rm -f "$testfile"; fi
    rm -rf "$work"
}
trap cleanup EXIT HUP INT TERM

# Compute exact expected target files from HEAD in an isolated directory. Accept
# only pristine targets or this exact patch, never unrelated target-file edits.
mkdir -p "$work/expected/server/grants" "$work/expected/server/tokens"
git -C "$work/expected" init -q
git -C "$dex" show HEAD:server/grants/refresh.go > "$work/original-refresh.go"
cp "$work/original-refresh.go" "$work/expected/server/grants/refresh.go"
git -C "$dex" show HEAD:server/tokens/refresh_store.go > "$work/expected/server/tokens/refresh_store.go"
git -C "$dex" show HEAD:server/tokens/refresh_store_test.go > "$work/expected/server/tokens/refresh_store_test.go"
git -C "$dex" show HEAD:server/server_test.go > "$work/expected/server/server_test.go"
git -C "$work/expected" apply "$here/dex-refresh-read-order.patch"
git -C "$work/expected" apply "$here/dex-issue-10-independent-grants.patch"
if cmp -s "$work/expected/server/grants/refresh.go" "$dex/server/grants/refresh.go" &&
   cmp -s "$work/expected/server/tokens/refresh_store.go" "$dex/server/tokens/refresh_store.go" &&
   cmp -s "$work/expected/server/weechat_sqlite_deadlock_test.go" "$dex/server/weechat_sqlite_deadlock_test.go" &&
   cmp -s "$work/expected/server/weechat_independent_grants_test.go" "$dex/server/weechat_independent_grants_test.go" &&
   cmp -s "$work/expected/server/weechat_shared_connector_test.go" "$dex/server/weechat_shared_connector_test.go"; then
    echo "Verified already-applied Dex read-order and issue #10 patches."
elif cmp -s "$work/original-refresh.go" "$dex/server/grants/refresh.go" &&
     [ ! -e "$dex/server/weechat_sqlite_deadlock_test.go" ] &&
     [ ! -e "$dex/server/tokens/connlock.go" ]; then
    git -C "$dex" apply --check "$here/dex-refresh-read-order.patch"
    git -C "$dex" apply "$here/dex-refresh-read-order.patch"
    git -C "$dex" apply --check "$here/dex-issue-10-independent-grants.patch"
    git -C "$dex" apply "$here/dex-issue-10-independent-grants.patch"
    echo "Applied Dex read-order and issue #10 patches (independent grants + shared-refresh serialization)."
else
    echo "refusing unexpected edits to Dex patch target files" >&2
    exit 1
fi
if [ -e "$testfile" ]; then
    cmp -s "$here/weechat_authority_acceptance_test.go" "$testfile" || {
        echo "refusing to overwrite an existing different acceptance test" >&2
        exit 1
    }
else
    cp "$here/weechat_authority_acceptance_test.go" "$testfile"
    created_test=true
fi
mkdir "$work/refreshcoord"
cp "$coordinator" "$work/refreshcoord/coordinator.go"
oauth_version=$(awk '$1 == "golang.org/x/oauth2" { print $2; exit }' "$dex/go.mod")
sys_version=$(awk '$1 == "golang.org/x/sys" { print $2; exit }' "$dex/go.mod")
[ -n "$oauth_version" ] && [ -n "$sys_version" ] || {
    echo "Dex go.mod is missing expected OAuth2/sys dependencies" >&2
    exit 1
}
cat > "$work/refreshcoord/go.mod" <<MOD
module acceptance.local/refreshcoord

go 1.27.0

require (
 golang.org/x/oauth2 $oauth_version
 golang.org/x/sys $sys_version
)
MOD
cp "$dex/go.mod" "$work/dex.mod"
cp "$dex/go.sum" "$work/dex.sum"
cd "$dex"
"$go" mod edit -modfile="$work/dex.mod" \
    -require=acceptance.local/refreshcoord@v0.0.0 \
    -replace="acceptance.local/refreshcoord=$work/refreshcoord"
echo "Testing exact patched coordinator source:"
sha256sum "$coordinator" "$work/refreshcoord/coordinator.go"
"$go" test -p=2 -race -count=1 -timeout=120s \
    -tags 'weechat_acceptance weechat_sqlite_acceptance weechat_independent_grants weechat_shared_connector' \
    -mod=mod -modfile="$work/dex.mod" \
    ./server ./server/grants ./server/tokens

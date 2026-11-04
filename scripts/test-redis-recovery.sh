#!/bin/sh
# Exercise the actual Compose Redis command using an isolated project and volume.
set -eu

repo_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
project="recipes-redis-recovery-$(date +%s)-$$"
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/recipes-redis-recovery.XXXXXX")

compose() {
    docker compose --project-name "$project" --file "$repo_dir/docker-compose.yml" "$@"
}

cleanup() {
    result=$?
    trap - EXIT
    if [ "$result" -ne 0 ]; then
        compose logs --no-color --tail 80 redis || true
    fi
    # Only this script's unique project owns the volume being removed.
    if ! compose down --volumes --remove-orphans; then
        result=1
    fi
    rm -f "$test_dir/startup.log"
    rmdir "$test_dir"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

start_redis() {
    compose up --detach --no-deps --wait --wait-timeout 20 redis
}

append_zero_tail() {
    compose stop redis
    compose run --rm --no-deps -T --entrypoint /bin/sh redis -s -- "$1" <<'SH'
set -eu
name=$(awk '/ type i( |$)/ { file = $2 } END { print file }' \
    /data/appendonlydir/appendonly.aof.manifest)
test -n "$name"
dd if=/dev/zero bs=1 count="$1" >> "/data/appendonlydir/$name" 2>/dev/null
SH
}

check_saved_key() {
    test "$(compose exec -T redis redis-cli --raw GET recovery-test)" = persisted
}

start_redis
test "$(compose exec -T redis redis-cli --raw CONFIG GET appendfsync | tail -n 1)" = always
test "$(compose exec -T redis redis-cli --raw CONFIG GET aof-load-corrupt-tail-max-size | tail -n 1)" = 4096
test "$(compose exec -T redis redis-cli --raw SET recovery-test persisted)" = OK

# A clean restart must preserve an acknowledged write.
compose stop redis
start_redis
check_saved_key
echo 'PASS: clean restart preserves the acknowledged key'

# Redis 8.10.2 uses a strict less-than comparison for the recovery threshold.
for bytes in 535 4095; do
    append_zero_tail "$bytes"
    start_redis
    check_saved_key
    echo "PASS: recovered $bytes zero bytes and preserved the acknowledged key"
done

# The safety limit must fail closed at 4 KiB instead of discarding it.
append_zero_tail 4096
if start_redis > "$test_dir/startup.log" 2>&1; then
    echo 'FAIL: Redis accepted corruption at the configured limit' >&2
    exit 1
fi
compose logs --no-color redis | grep -F 'Bad file format reading the append only file'
echo 'PASS: corruption of 4096 bytes requires manual recovery'

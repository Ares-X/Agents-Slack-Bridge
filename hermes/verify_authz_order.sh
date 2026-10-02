#!/usr/bin/env bash
# 核验 README「授权模型」声明的调用顺序是否与固定上游版本源码一致。
# 用法: ./verify_authz_order.sh [hermes-agent 仓库路径] [commit]
# 默认: ~/.hermes/hermes-agent + b3059921bc（README 标注的核对版本）。
#
# 注意: 本脚本用临时文件而非 `printf | grep -q` 管道——后者在 pipefail 下会因
# grep -q 提前退出导致 printf 收 SIGPIPE、管道返回 141 而误报失配。
set -euo pipefail

REPO="${1:-$HOME/.hermes/hermes-agent}"
COMMIT="${2:-b3059921bc}"

fail() { echo "VERIFY-FAIL: $1"; exit 1; }

if [ ! -d "$REPO/.git" ]; then
    echo '{"error": "repo not found: '"$REPO"'"}' >&2
    exit 1
fi
cd "$REPO"
git cat-file -t "$COMMIT" >/dev/null 2>&1 || { echo '{"error": "commit not found: '"$COMMIT"'"}' >&2; exit 1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git show "$COMMIT":plugins/platforms/slack/adapter.py > "$TMP/adapter.py"
git show "$COMMIT":gateway/authz_mixin.py     > "$TMP/authz.py"
git show "$COMMIT":gateway/platforms/base.py  > "$TMP/base.py"

# 1) _drop_bot_sender（allow_bots gate）在 _early_reject_unauthorized 之前
DROP_LINE=$(grep -n 'if await self\._drop_bot_sender' "$TMP/adapter.py" | head -1 | cut -d: -f1)
EARLY_LINE=$(grep -n 'if self\._early_reject_unauthorized' "$TMP/adapter.py" | head -1 | cut -d: -f1)
[ -n "$DROP_LINE" ] || fail "call to _drop_bot_sender not found"
[ -n "$EARLY_LINE" ] || fail "call to _early_reject_unauthorized not found"
[ "$DROP_LINE" -lt "$EARLY_LINE" ] || fail "_drop_bot_sender($DROP_LINE) not before _early_reject($EARLY_LINE)"

# 2) 早期检查签名不带 is_bot
grep -q 'def _early_reject_unauthorized(self, user_id: str, channel_id: str, is_dm: bool) -> bool:' \
    "$TMP/adapter.py" \
    || fail "_early_reject_unauthorized signature changed (expected no is_bot param)"

# 3) 调用 _is_sender_authorized 时不传 is_bot
grep -A3 'decision = (' "$TMP/adapter.py" | grep -q 'self\._is_sender_authorized(user_id, chat_type, channel_id)' \
    || fail "_is_sender_authorized call passes more than (user_id, chat_type, channel_id)"

# 4) build_source 默认 is_bot=False（早期检查的 fallback source 不带 bot 标记）
grep -q 'is_bot: bool = False' "$TMP/base.py" \
    || fail "build_source default is_bot=False not found in base.py"

# 5) MessageEvent 构建时才置 is_bot（在两个 gate 之后）
grep -q 'is_bot=self\._event_declares_bot_sender(event)' "$TMP/adapter.py" \
    || fail "MessageEvent is_bot assignment not found"

# 6) _chat_scoped_grant 在 _principal_authorized 内、no-user-id guard 之前被调用（ALLOW_BOTS 短路可达）
GRANT_DEF=$(grep -n 'def _chat_scoped_grant' "$TMP/authz.py" | head -1 | cut -d: -f1)
PRINCIPAL_DEF=$(grep -n 'def _principal_authorized' "$TMP/authz.py" | head -1 | cut -d: -f1)
[ -n "$GRANT_DEF" ] && [ -n "$PRINCIPAL_DEF" ] || fail "grant/principal defs not found"
GRANT_CALL=$(awk -v s="$PRINCIPAL_DEF" 'NR>s && /_chat_scoped_grant\(source/ {print NR; exit}' "$TMP/authz.py")
NOUID_GUARD=$(awk -v s="$PRINCIPAL_DEF" 'NR>s && /if not user_id:/ {print NR; exit}' "$TMP/authz.py")
[ -n "$GRANT_CALL" ] || fail "_chat_scoped_grant call not found in _principal_authorized"
if [ -n "$NOUID_GUARD" ]; then
    [ "$GRANT_CALL" -lt "$NOUID_GUARD" ] || fail "ALLOW_BOTS grant($GRANT_CALL) not before no-user-id guard($NOUID_GUARD)"
fi

# 7) _is_user_authorized：非 bot source 在 principal 之后直接放行，bot source 过 loop guard
grep -q 'if not getattr(source, "is_bot", False):' "$TMP/authz.py" \
    || fail "is_bot short-circuit not found in _is_user_authorized"

echo "VERIFY-OK: authz order claims match $COMMIT (drop_bot_sender@$DROP_LINE < early_reject@$EARLY_LINE; grant@$GRANT_CALL < no-user-id-guard@${NOUID_GUARD:-n/a})"

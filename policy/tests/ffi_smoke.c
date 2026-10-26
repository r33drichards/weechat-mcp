/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "weechat_managed_policy.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

#define BYTES(value) ((const uint8_t *)(value)), strlen(value)

int main(void)
{
    char source[] = "package weechat.managed\nallow := input.operation == \"log\"";
    const char data[] = "{}";
    const char allow[] = "{\"principal\":{\"id\":\"example\",\"generation\":1,\"trust\":\"managed\"},\"phase\":\"active\",\"operation\":\"log\",\"resource\":{},\"request\":{\"text\":\"hello\"}}";
    const char deny[] = "{\"principal\":{\"id\":\"example\",\"generation\":1,\"trust\":\"managed\"},\"phase\":\"active\",\"operation\":\"command\",\"resource\":{},\"request\":{}}";
    struct weechat_managed_policy *policy = weechat_managed_policy_load(BYTES(source), BYTES(data));
    assert(policy != NULL);
    /* Source is copied, not kept as a caller-owned mutable authority string. */
    memset(source, 'x', sizeof(source) - 1);
    assert(weechat_managed_policy_authorize(policy, BYTES(allow)) == WEECHAT_MANAGED_POLICY_ALLOW);
    assert(weechat_managed_policy_authorize(policy, BYTES(deny)) == WEECHAT_MANAGED_POLICY_DENY);
    assert(weechat_managed_policy_authorize(policy, BYTES("not json")) == WEECHAT_MANAGED_POLICY_ERROR);
    assert(weechat_managed_policy_authorize(NULL, BYTES(allow)) == WEECHAT_MANAGED_POLICY_ERROR);
    weechat_managed_policy_free(policy);
    weechat_managed_policy_free(NULL);
    puts("embedded policy C ABI passed: allow, deny, error, immutable source");
    return 0;
}

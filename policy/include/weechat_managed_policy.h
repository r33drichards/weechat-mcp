/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef WEECHAT_MANAGED_POLICY_H
#define WEECHAT_MANAGED_POLICY_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque native authority. Never expose this pointer or any of these functions
 * to managed JavaScript or its MCP filesystem/tool connection. */
struct weechat_managed_policy;

enum weechat_managed_policy_decision {
    WEECHAT_MANAGED_POLICY_ERROR = -1,
    WEECHAT_MANAGED_POLICY_DENY = 0,
    WEECHAT_MANAGED_POLICY_ALLOW = 1
};

#define WEECHAT_MANAGED_POLICY_MAX_SOURCE_BYTES 65536u
#define WEECHAT_MANAGED_POLICY_MAX_DATA_BYTES 65536u
#define WEECHAT_MANAGED_POLICY_MAX_INPUT_BYTES 32768u

/* UTF-8 buffers are length-delimited, need not have trailing NULs, and must stay
 * readable until the call returns. Returns NULL on invalid input/load failure.
 * Source/data are copied once and immutable thereafter; no mutation API exists.
 * The host must load them from operator-owned paths outside managed code roots.
 */
struct weechat_managed_policy *weechat_managed_policy_load(
    const uint8_t *source, size_t source_len,
    const uint8_t *data_json, size_t data_len);

/* Accepts only the documented, bounded native input envelope. The principal
 * must be derived by the host from actual V8 Context metadata, NEVER from JS.
 * Only the literal return value ALLOW grants an effect. DENY and ERROR both
 * prohibit it. No evaluator mutex remains held after this function returns.
 * The caller must not race free with authorization or pass invalid pointers.
 */
int weechat_managed_policy_authorize(
    const struct weechat_managed_policy *policy,
    const uint8_t *input_json, size_t input_len);

/* NULL is safe; other handles must come from load and be freed exactly once. */
void weechat_managed_policy_free(struct weechat_managed_policy *policy);

#ifdef __cplusplus
}
#endif
#endif

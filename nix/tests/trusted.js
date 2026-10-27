// SPDX-License-Identifier: GPL-3.0-or-later
// Legacy trusted JavaScript intentionally retains the full WeeChat API.
function trusted_process_done(data, command, returnCode, output, error) {
    if (returnCode === 0) {
        weechat.command(data, "/command irc /msg * trusted:process:" + output);
    }
    return weechat.WEECHAT_RC_OK;
}
function trusted_print(data, buffer, date, tags, displayed, highlight, prefix, message) {
    if (tags.split(",").indexOf("self_msg") !== -1) {
        return weechat.WEECHAT_RC_OK;
    }
    if (message === "trusted-ping") {
        weechat.command(buffer, "/command irc /msg * trusted:pong");
    } else if (message === "trusted-promote") {
        // Classification must prevent managed source from entering full bindings.
        weechat.command("", "/javascript load /var/lib/weechat-managed-js/sources/escape.js");
        weechat.command(buffer, "/command irc /msg * trusted:promote-attempted");
    } else if (message === "trusted-process") {
        // Harmless external program; proves managed restrictions are not global.
        weechat.hook_process("printf trusted-process-ok", 5000, "trusted_process_done", buffer);
    }
    return weechat.WEECHAT_RC_OK;
}
if (weechat.register("trusted_compat", "test", "1", "GPL3", "VM compatibility test", "", "")) {
    weechat.hook_print("", "irc_privmsg", "", 1, "trusted_print", "");
}

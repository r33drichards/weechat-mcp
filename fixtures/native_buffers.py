# SPDX-License-Identifier: GPL-3.0-or-later
"""Only loaded by native_buffer_live_test.py in a disposable, offline profile."""

import json
import os
from pathlib import Path

import weechat as w  # ty: ignore[unresolved-import]

w.register(
    "native_fixture", "test", "1", "GPL3", "offline native buffer fixture", "", ""
)
buffer = w.buffer_new("native_fixture", "", "", "", "")
control = Path(os.environ["NATIVE_TEST_CONTROL"])
response = control.with_suffix(".result")
repo = os.environ["NATIVE_TEST_REPO"]
w.command("", "/set plugins.var.python.mcp.buffers python.native_fixture")
for i in range(int(os.environ.get("NATIVE_TEST_SEED", "650"))):
    w.prnt_date_tags(
        buffer, 1700000000 + i, "nick_alice,irc_privmsg", "alice\told-" + str(i)
    )


def tick(data, remaining):
    if not control.exists():
        return w.WEECHAT_RC_OK
    request = json.loads(control.read_text())
    control.unlink()
    try:
        action = request["action"]
        if action == "append":
            w.prnt_date_tags(
                buffer, 1, "nick_alice,irc_privmsg,hidden", "alice\tnew-backdated"
            )
        elif action == "filter":
            w.command("", "/filter add native_hidden python.native_fixture hidden *")
        elif action == "clear":
            w.buffer_clear(buffer)
        elif action == "prune":
            w.command("", "/set weechat.history.max_buffer_lines_number 100")
            for i in range(150):
                w.prnt_date_tags(
                    buffer,
                    1700001000 + i,
                    "nick_alice,irc_privmsg",
                    "alice\tprune-" + str(i),
                )
        elif action == "reload":
            # Schedule outside this Python callback: unloading another Python
            # interpreter reentrantly is unsafe in WeeChat 4.6.3.
            w.command("", "/wait 1 /python unload mcp")
            w.command("", "/wait 2 /python load " + repo + "/weechat_mcp.py")
        else:
            raise ValueError("Unknown fixture action")
        result = {"id": request["id"], "ok": True}
    except Exception as error:
        result = {"id": request["id"], "error": str(error)}
    # The test polls this file while the callback runs. Never expose a truncated
    # result, including failures, before the complete response is ready.
    temporary = response.with_name(response.name + ".tmp")
    temporary.write_text(json.dumps(result))
    temporary.replace(response)
    return w.WEECHAT_RC_OK


w.hook_timer(50, 0, 0, "tick", "")

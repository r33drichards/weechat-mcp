// SPDX-License-Identifier: GPL-3.0-or-later
// No Node.js globals, filesystem, policy administration, or raw WeeChat API.
weechat.onMessage(({ buffer, text }) => {
  if (text === "!ping") {
    weechat.send(buffer, "pong"); // Queued intent, not delivery confirmation.
  } else if (text === "!example") {
    weechat.http("GET", "https://example.com/", "", (response) => {
      if (response.error) {
        weechat.send(buffer, `HTTP failed (${response.outcome}); not retried`);
      } else {
        weechat.send(buffer, `HTTP status ${response.result.status}`);
      }
    });
  }
});

// SPDX-License-Identifier: GPL-3.0-or-later
// Test source only: every effect must cross the independent native policy gate.
weechat.onMessage(function (event) {
    const buffer = event.buffer;
    const text = event.text;
    const reply = function (message) {
        if (message.error) {
            weechat.send(buffer, "http-error:" + message.error + ":" + message.outcome);
        } else {
            const result = message.result;
            weechat.send(buffer, "http:" + result.status + ":" + result.body);
        }
    };
    if (text === "ping") {
        weechat.send(buffer, "v1:pong");
    } else if (text === "deny") {
        const missing = [
            typeof weechat.command,
            typeof weechat.hook_process,
            typeof weechat.config_get,
            typeof weechat.info_get,
            typeof weechat.readFile,
            typeof require,
            typeof process,
        ].filter(function (value) { return value === "undefined"; }).length;
        weechat.send(buffer, "denied:" + missing);
    } else if (text === "literal") {
        weechat.send(buffer, "literal:${process.env.HOME};weechat.command('/quit')");
    } else if (text === "slash") {
        weechat.send(buffer, "/exec /bin/true");
    } else if (text === "slash-join") {
        weechat.send(buffer, "/JOIN #not-joined");
    } else if (text === "double-slash") {
        weechat.send(buffer, "//literal");
    } else if (text === "dotpath") {
        weechat.http("GET", "http://fixture.test:18080/public/../admin", "", reply);
    } else if (text === "encoded-dotpath") {
        weechat.http("GET", "http://fixture.test:18080/public/%2e%2e/admin", "", reply);
    } else if (text === "scope") {
        // The native buffer exists, but the immutable managed policy denies it.
        weechat.send("irc.managed.#outside", "forbidden-managed-send");
    } else if (text === "outside-trigger") {
        weechat.send("irc.managed.#test", "forbidden-outside-event");
    } else if (text === "loop") {
        while (true) {}
    } else if (text === "microtask-loop") {
        Promise.resolve().then(function () { while (true) {} });
    } else if (text.indexOf("http-") === 0) {
        const method = text.slice(5).toUpperCase();
        weechat.http(method, "http://fixture.test:18080/echo", method === "GET" ? "" : "payload", reply);
    } else if (text === "private-http") {
        weechat.http("GET", "http://127.0.0.1:16667/", "", reply);
    } else if (text === "policy-http") {
        weechat.http("GET", "http://outside.test:18080/policy-denied", "", reply);
    } else if (text === "redirect") {
        weechat.http("GET", "http://fixture.test:18080/redirect", "", reply);
    } else if (text === "large") {
        weechat.http("GET", "http://fixture.test:18080/large", "", reply);
    } else if (text === "slow") {
        weechat.http("GET", "http://fixture.test:18080/slow", "", reply);
    } else if (text === "tls") {
        weechat.http("GET", "https://fixture.test:18443/tls", "", reply);
    } else if (text === "tls-wrong-host") {
        weechat.http("GET", "https://wrong.fixture.test:18443/tls", "", reply);
    }
});

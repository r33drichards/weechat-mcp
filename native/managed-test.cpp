// SPDX-License-Identifier: GPL-3.0-or-later
// Real V8 + Regorus, fake WeeChat API: the VM tests the actual native host.
#include <cassert>
#include <cstdarg>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <libplatform/libplatform.h>
#include "weechat-js-managed.cpp"

struct t_hashtable { std::map<std::string, std::string> values; };
struct t_weechat_plugin api{};
struct t_weechat_plugin *weechat_js_plugin = &api;
v8::Isolate *js_isolate = nullptr;
v8::Platform *js_platform = nullptr;
static std::unique_ptr<v8::Platform> test_platform;
static v8::ArrayBuffer::Allocator *test_allocator;
static std::vector<std::pair<std::string, std::string>> sends;
static std::vector<std::string> requests, canceled;
static std::string normalize_host = "example.com";
static bool unload_on_send = false;

void weechat_js_v8_init() {
    if (js_isolate) return;
    v8::V8::SetFlagsFromString("--single-threaded");
    test_platform = v8::platform::NewSingleThreadedDefaultPlatform();
    js_platform = test_platform.get();
    v8::V8::InitializePlatform(js_platform);
    assert(v8::V8::Initialize());
    test_allocator = v8::ArrayBuffer::Allocator::NewDefaultAllocator();
    v8::Isolate::CreateParams params;
    params.array_buffer_allocator = test_allocator;
    js_isolate = v8::Isolate::New(params);
    js_isolate->Enter();
}

static void fake_print(t_gui_buffer *, time_t, int, const char *, const char *, ...) {}

static void native_api() {
    api.file_get_content = [](const char *path) -> char * {
        std::ifstream file(path);
        std::string content((std::istreambuf_iterator<char>(file)), {});
        return file.bad() ? nullptr : strdup(content.c_str());
    };
    api.hashtable_new = [](int, const char *, const char *, unsigned long long (*)(t_hashtable *, const void *), int (*)(t_hashtable *, const void *, const void *)) { return new t_hashtable; };
    api.hashtable_get = [](t_hashtable *value, const void *key) -> void * {
        if (!value) return nullptr;
        auto found = value->values.find(static_cast<const char *>(key));
        return found == value->values.end() ? nullptr : const_cast<char *>(found->second.c_str());
    };
    api.hashtable_set = [](t_hashtable *value, const void *key, const void *entry) -> t_hashtable_item * {
        value->values[static_cast<const char *>(key)] = static_cast<const char *>(entry);
        return reinterpret_cast<t_hashtable_item *>(value);
    };
    api.hashtable_free = [](t_hashtable *value) { delete value; };
    api.printf_datetime_tags = &fake_print;
    api.hook_timer = [](t_weechat_plugin *, long, int, int, int (*)(const void *, void *, int), const void *, void *) { return reinterpret_cast<t_hook *>(1); };
    api.hook_info_hashtable = [](t_weechat_plugin *, const char *, const char *, const char *, const char *, t_hashtable *(*)(const void *, void *, const char *, t_hashtable *), const void *, void *) { return reinterpret_cast<t_hook *>(1); };
    api.info_get_hashtable = [](t_weechat_plugin *, const char *operation, t_hashtable *input) -> t_hashtable * {
        std::string op(operation);
        if (op == "mcp_managed_send") {
            sends.emplace_back(field(input, "buffer"), field(input, "text"));
            auto *result = table({{"ok", "1"}, {"accepted", "1"}});
            if (unload_on_send) { weechat_js_managed_end(); api = {}; }
            return result;
        }
        if (op == "mcp_managed_http_validate") {
            return table({{"ok", "1"}, {"method", field(input, "method")}, {"url", field(input, "url")}, {"body", field(input, "body")}, {"scheme", "https"}, {"host", normalize_host}, {"port", "443"}});
        }
        if (op == "mcp_managed_http_start") { requests.push_back(field(input, "id")); return table({{"ok", "1"}}); }
        if (op == "mcp_managed_http_cancel") { canceled.push_back(field(input, "id")); return table({{"ok", "1"}}); }
        return nullptr;
    };
}

static std::map<std::string, std::string> call(const char *name, const std::map<std::string, std::string> &input = {}) {
    auto *args = table(input);
    auto *result = manager(nullptr, nullptr, name, args);
    auto values = result->values;
    delete args; delete result;
    return values;
}
static void activate(const char *source, bool accepted = true) {
    next_candidate = Clock::time_point{}; // Independent fixture operation.
    auto result = call("javascript_managed_activate", {{"name", "example"}, {"source", source}, {"digest", std::string(64, 'a')}});
    assert((result["ok"] == "1") == accepted);
}
static void message_event(const char *message = "ping") {
    send_times.clear();
    call("javascript_managed_message", {{"buffer", "irc.test.#allowed"}, {"nick", "alice"}, {"text", message}});
    timer(nullptr, nullptr, 0);
}
static void readonly(const std::filesystem::path &path, const std::string &text) {
    std::ofstream file(path); file << text; file.close();
    std::filesystem::permissions(path, std::filesystem::perms::owner_read | std::filesystem::perms::group_read | std::filesystem::perms::others_read);
}

int main(int argc, char **argv) {
    assert(argc == 2);
    native_api();
    std::filesystem::path root = std::filesystem::temp_directory_path() / ("managed-native-" + std::to_string(getpid()));
    std::filesystem::create_directories(root / "sources");
    std::filesystem::permissions(root / "sources", std::filesystem::perms::owner_all);
    std::ifstream policy_source(argv[1]);
    std::string rego((std::istreambuf_iterator<char>(policy_source)), {});
    readonly(root / "policy.rego", rego);
    readonly(root / "data.json", R"({"managed":{"buffers":["irc.test.#allowed"],"http_methods":["GET"],"http_hosts":["example.com"],"log":true}})");
    setenv("WEECHAT_MCP_MANAGED_JS", "1", 1);
    setenv("WEECHAT_MANAGED_ROOT", (root / "sources").c_str(), 1);
    setenv("WEECHAT_MANAGED_POLICY", (root / "policy.rego").c_str(), 1);
    setenv("WEECHAT_MANAGED_DATA", (root / "data.json").c_str(), 1);
    assert(weechat_js_managed_init());
    activate("weechat.onMessage(e => weechat.send(e.buffer, e.text));");
    message_event("[exec]; literal ${text}");
    assert(sends.size() == 1 && sends.back().second == "[exec]; literal ${text}");
    auto initial = plugins.at("example")->generation;
    activate("weechat.send('irc.test.#allowed','staged'); weechat.onMessage(()=>{});", false);
    assert(plugins.at("example")->generation == initial && sends.size() == 1);
    activate("weechat.onMessage(e => weechat.send('irc.test.#outside', 'forbidden')); ");
    message_event(); assert(sends.size() == 1);
    activate("weechat.onMessage(e => { if (Object.keys(weechat).sort().join(',') !== 'http,log,onMessage,send' || typeof process !== 'undefined' || typeof require !== 'undefined' || typeof WebAssembly !== 'undefined' || typeof SharedArrayBuffer !== 'undefined' || typeof Atomics !== 'undefined') throw Error('leak'); weechat.send(e.buffer,'narrow'); });");
    message_event(); assert(sends.back().second == "narrow");
    activate("weechat.onMessage(e => { weechat.http('GET','https://example.com/','',r => weechat.send(e.buffer, r.result.body)); });");
    message_event(); assert(requests.size() == 1);
    call("javascript_managed_http_complete", {{"id", requests.back()}, {"response_json", R"({"id":"1","result":{"body":"done","status":200}})"}});
    timer(nullptr, nullptr, 0); assert(sends.back().second == "done");
    normalize_host = "forbidden.example";
    activate("weechat.onMessage(e => weechat.http('GET','https://forbidden.example/','',r => weechat.send(e.buffer,r.error)));");
    message_event(); timer(nullptr, nullptr, 0);
    assert(requests.size() == 1 && sends.back().second == "policy_denied");
    normalize_host = "example.com";
    activate("weechat.onMessage(e => weechat.http('GET','https://example.com/','',r => weechat.send(e.buffer,'stale')));");
    message_event(); assert(requests.size() == 2);
    std::string stale = requests.back();
    activate("weechat.onMessage(e => weechat.send(e.buffer,'fresh'));");
    timer(nullptr, nullptr, 0); // Best-effort cancellation is deferred, never destructor reentry.
    assert(canceled.back() == stale);
    size_t before = sends.size();
    call("javascript_managed_http_complete", {{"id", stale}, {"response_json", "{}"}});
    timer(nullptr, nullptr, 0); assert(sends.size() == before);
    activate("weechat.onMessage(e => { weechat.http('GET','https://example.com/','',()=>{}); throw Error('drop'); });");
    message_event(); assert(plugins.at("example")->pending.empty() && requests.size() == 2);
    activate("weechat.onMessage(e => { if(e.text === 'first') weechat.http('GET','https://example.com/','',()=>{}); else while(true){} });");
    message_event("first");
    auto pending_id = requests.back();
    auto start = Clock::now(); message_event();
    assert(Clock::now() - start < std::chrono::seconds(2) && !plugins.at("example")->active);
    timer(nullptr, nullptr, 0);
    assert(plugins.at("example")->pending.empty() && canceled.back() == pending_id);
    activate("weechat.onMessage(e => weechat.send(e.buffer,'recovered'));");
    message_event(); assert(sends.back().second == "recovered");
    for (int i = 0; i < 100; ++i) call("javascript_managed_message", {{"buffer", "irc.test.#allowed"}, {"nick", "alice"}, {"text", "queued"}});
    assert(plugins.at("example")->jobs.size() == QUEUE_LIMIT);
    assert(call("javascript_managed_validate", {{"name", "-wrong"}, {"source", "weechat.onMessage(()=>{});"}, {"digest", std::string(64,'a')}})["error"] == "invalid_source");
    next_candidate = Clock::time_point{};
    auto exhausted = call("javascript_managed_validate", {{"name", "busy"}, {"source", "while(true){}"}, {"digest", std::string(64,'b')}});
    assert(exhausted["error"] == "candidate_rejected");
    auto flood_started = Clock::now();
    for (int i = 0; i < 100; ++i)
        assert(call("javascript_managed_validate", {{"name", "busy"}, {"source", "while(true){}"}, {"digest", std::string(64,'b')}})["error"] == "candidate_rate_limited");
    assert(Clock::now() - flood_started < std::chrono::milliseconds(100));
    std::ofstream(root / "sources" / "untrusted.js") << "weechat.command('', '/exec bad')";
    assert(weechat_js_managed_path((root / "sources" / "untrusted.js").c_str()));
    std::filesystem::create_symlink(root / "sources" / "untrusted.js", root / "escape.js");
    assert(weechat_js_managed_path((root / "escape.js").c_str()));
    bool handled = false;
    assert(weechat_js_managed_read_trusted((root / "escape.js").c_str(), &handled) == nullptr && handled);
    std::filesystem::create_hard_link(root / "sources" / "untrusted.js", root / "hardlink.js");
    assert(weechat_js_managed_read_trusted((root / "hardlink.js").c_str(), &handled) == nullptr);
    std::ofstream(root / "trusted.js") << "trusted";
    std::filesystem::create_symlink(root / "trusted.js", root / "trusted-link.js");
    char *trusted_source = weechat_js_managed_read_trusted((root / "trusted-link.js").c_str(), &handled);
    assert(trusted_source && std::string(trusted_source) == "trusted");
    free(trusted_source);
    std::filesystem::create_hard_link(root / "trusted.js", root / "trusted-hard.js");
    trusted_source = weechat_js_managed_read_trusted((root / "trusted-hard.js").c_str(), &handled);
    assert(trusted_source && std::string(trusted_source) == "trusted");
    free(trusted_source);
    std::filesystem::rename(root / "sources", root / "moved-sources");
    assert(weechat_js_managed_path((root / "moved-sources" / "untrusted.js").c_str()));
    std::filesystem::rename(root / "moved-sources", root / "sources");
    // Preserve legacy isolate liveness after all managed terminations.
    { Isolate::Scope legacy(js_isolate); HandleScope handles(js_isolate); auto ctx = Context::New(js_isolate); Context::Scope scope(ctx);
      auto text = String::NewFromUtf8Literal(js_isolate, "21*2"); auto script = Script::Compile(ctx,text).ToLocalChecked();
      assert(script->Run(ctx).ToLocalChecked()->Int32Value(ctx).FromJust() == 42); }
    activate("weechat.onMessage(e => { if(e.text === 'first') weechat.http('GET','https://example.com/','',()=>{}); else { weechat.send(e.buffer,'before-unload'); weechat.send(e.buffer,'must-not-send'); } });");
    message_event("first");
    size_t send_count = sends.size();
    unload_on_send = true;
    message_event("unload"); // Clears the entire fake plugin vtable while draining.
    assert(!enabled && plugins.empty() && sends.size() == send_count + 1);
    assert(sends.back().second == "before-unload");
    unload_on_send = false;
    native_api();
    weechat_js_managed_end();
    // Missing policy is fail-closed, not empty-policy allow.
    setenv("WEECHAT_MANAGED_POLICY", "/missing-policy.rego", 1);
    assert(!weechat_js_managed_init());
    assert(call("javascript_managed_status")["error"] == "managed_unavailable");
    js_isolate->Exit(); js_isolate->Dispose(); delete test_allocator;
    V8::Dispose(); V8::DisposePlatform();
    std::filesystem::remove_all(root);
    std::cout << "PASS: real V8+Rego native manager policy/scope/queue/reload/timeouts; fake WeeChat transport\n";
}

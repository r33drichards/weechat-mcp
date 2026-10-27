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

struct FakeTimer {
    long interval;
    int remaining;
    int (*callback)(const void *, void *, int);
    const void *pointer;
    void *data;
};
static std::map<t_hook *, std::unique_ptr<FakeTimer>> fake_timers;
static size_t timer_registrations = 0;
static int fail_timer_after = -1;
static void fire_timer(t_hook *hook) {
    auto found = fake_timers.find(hook);
    if (found == fake_timers.end()) return;
    // Snapshot: callback may unhook itself or unload the plugin synchronously.
    FakeTimer value = *found->second;
    value.callback(value.pointer, value.data, value.remaining > 0 ? value.remaining - 1 : -1);
    found = fake_timers.find(hook);
    if (found != fake_timers.end() && found->second->remaining > 0 && --found->second->remaining == 0)
        fake_timers.erase(found);
}
static std::vector<t_hook *> managed_hooks() {
    std::vector<t_hook *> result;
    for (const auto &[hook, value] : fake_timers)
        if (value->callback == &managed_timer) result.push_back(hook);
    return result;
}
static void dispatch_timer() {
    send_times.clear();
    timer(nullptr, nullptr, 0);
}

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
    api.hook_timer = [](t_weechat_plugin *, long interval, int align, int remaining,
                        int (*callback)(const void *, void *, int), const void *pointer, void *data) -> t_hook * {
        assert(interval > 0 && align == 0 && data == nullptr);
        if (callback == &managed_timer && fail_timer_after >= 0) {
            if (fail_timer_after-- == 0) return nullptr;
        }
        auto value = std::make_unique<FakeTimer>(FakeTimer{interval, remaining, callback, pointer, data});
        auto *hook = reinterpret_cast<t_hook *>(value.get());
        fake_timers.emplace(hook, std::move(value));
        ++timer_registrations;
        return hook;
    };
    api.unhook = [](t_hook *hook) {
        assert(fake_timers.erase(hook) == 1); // Detect double unhook and stale one-shot handles.
    };
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


// Counters are observable under deny-all policy: no log/send/network permission.
static void assert_timer_status(size_t live, uint64_t started, uint64_t completed, uint64_t failed) {
    const auto status = call("javascript_managed_status");
    const auto expected = "\"timers\":{\"live\":" + std::to_string(live) +
        ",\"callbacks_started\":" + std::to_string(started) +
        ",\"callbacks_completed\":" + std::to_string(completed) +
        ",\"callbacks_failed\":" + std::to_string(failed) + "}";
    assert(status.at("ok") == "1" && status.at("plugins").find(expected) != std::string::npos);
}
static void timer_observability_tests() {
    const auto before_sends = sends.size(), before_requests = requests.size();
    activate("setTimeout(()=>{},100); weechat.onMessage(()=>{});");
    assert_timer_status(1, 0, 0, 0);
    auto hook = managed_hooks().front();
    fire_timer(hook);
    assert_timer_status(1, 0, 0, 0); // A native tick is not a callback execution.
    dispatch_timer();
    assert_timer_status(0, 1, 1, 0);
    assert(call("javascript_managed_status") == call("javascript_managed_status"));

    activate("let n=0;const id=setInterval(()=>{if(++n===2)clearInterval(id);},100);weechat.onMessage(()=>{});");
    assert_timer_status(1, 0, 0, 0); // Replacement is a new generation.
    hook = managed_hooks().front();
    for (int i = 0; i < 5; ++i) fire_timer(hook);
    dispatch_timer(); assert_timer_status(1, 1, 1, 0); // Coalesced ticks.
    fire_timer(hook); dispatch_timer(); assert_timer_status(0, 2, 2, 0);
    const auto last_good = call("javascript_managed_status");
    activate("setTimeout(()=>{},100); throw Error('candidate rejected');", false);
    assert(call("javascript_managed_status") == last_good);
    next_candidate = Clock::time_point{};
    assert(call("javascript_managed_validate", {{"name", "validated"},
        {"source", "setTimeout(()=>{},100);weechat.onMessage(()=>{});"},
        {"digest", std::string(64, 'a')}})["ok"] == "1");
    assert(call("javascript_managed_status") == last_good);

    activate("const id=setTimeout(()=>{throw Error('must not run');},100);clearTimeout(id);weechat.onMessage(()=>{});");
    dispatch_timer(); assert_timer_status(0, 0, 0, 0);
    activate("setTimeout(()=>{},100);weechat.onMessage(()=>{});");
    fire_timer(managed_hooks().front());
    activate("weechat.onMessage(()=>{});");
    dispatch_timer(); assert_timer_status(0, 0, 0, 0); // Old queued callback revoked.

    activate("setTimeout(()=>{throw Error('failure');},100);weechat.onMessage(()=>{});");
    fire_timer(managed_hooks().front()); dispatch_timer(); assert_timer_status(0, 1, 0, 1);
    activate("setInterval(()=>{while(true){}},100);weechat.onMessage(()=>{});");
    fire_timer(managed_hooks().front()); dispatch_timer(); assert_timer_status(0, 1, 0, 1);
    assert(!plugins.at("example")->active);

    activate("setInterval(()=>{},100);weechat.onMessage(()=>{});");
    auto owner = plugins.at("example");
    owner->timer_callbacks_started = Managed::COUNTER_MAX - 1;
    owner->timer_callbacks_completed = Managed::COUNTER_MAX - 1;
    hook = managed_hooks().front();
    fire_timer(hook); dispatch_timer();
    fire_timer(hook); dispatch_timer();
    assert_timer_status(1, Managed::COUNTER_MAX, Managed::COUNTER_MAX, 0);
    assert(sends.size() == before_sends && requests.size() == before_requests);
    activate("weechat.onMessage(()=>{});");
}

static void timer_tests() {
    size_t before = sends.size();
    size_t registrations = timer_registrations;
    next_candidate = Clock::time_point{};
    auto result = call("javascript_managed_validate", {{"name", "validated"},
        {"source", "setTimeout(()=>{},100); weechat.onMessage(()=>{});"}, {"digest", std::string(64, 'a')}});
    assert(result["ok"] == "1" && timer_registrations == registrations && managed_hooks().empty());
    activate("setTimeout(()=>weechat.send('irc.test.#allowed','once'),100); weechat.onMessage(()=>{});");
    auto hooks = managed_hooks(); assert(hooks.size() == 1);
    assert(fake_timers.at(hooks[0])->interval == 100 && fake_timers.at(hooks[0])->remaining == 1);
    fire_timer(hooks[0]);
    assert(sends.size() == before && managed_hooks().empty()); // Native hook only queues.
    dispatch_timer(); assert(sends.size() == before + 1 && sends.back().second == "once");
    dispatch_timer(); assert(sends.size() == before + 1 && native_timers.empty());

    activate("let count=0; const id=setInterval(()=>{ weechat.send('irc.test.#allowed','interval:'+ ++count); clearTimeout(id); },100); weechat.onMessage(()=>{});");
    hooks = managed_hooks(); assert(hooks.size() == 1 && fake_timers.at(hooks[0])->remaining == 0);
    for (int i = 0; i < 10; ++i) fire_timer(hooks[0]);
    assert(plugins.at("example")->jobs.size() == 1);
    dispatch_timer(); assert(sends.back().second == "interval:1" && plugins.at("example")->timers.empty());
    dispatch_timer(); assert(managed_hooks().empty() && native_timers.empty());

    activate("const id=setInterval(()=>weechat.send('irc.test.#allowed','survived'),100); weechat.onMessage(e=>{ clearInterval(id); setTimeout(()=>weechat.send(e.buffer,'rollback-leak'),100); throw Error('rollback'); });");
    hooks = managed_hooks();
    message_event();
    assert(plugins.at("example")->timers.size() == 1 && managed_hooks().size() == 1);
    fire_timer(hooks[0]); dispatch_timer(); assert(sends.back().second == "survived");

    // Invalid candidate cannot displace the active generation or add hooks.
    auto generation = plugins.at("example")->generation;
    registrations = timer_registrations;
    activate("setTimeout(()=>{},100); throw Error('bad candidate');", false);
    assert(plugins.at("example")->generation == generation && timer_registrations == registrations);
    fail_timer_after = 1; // First candidate hook succeeds, second allocation fails.
    activate("setTimeout(()=>{},100); setInterval(()=>{},100); weechat.onMessage(()=>{});", false);
    fail_timer_after = -1;
    assert(plugins.at("example")->generation == generation);
    assert(managed_hooks().size() == 1 && native_timers.size() == 1);
    fire_timer(hooks[0]); // Queued work from old generation must not survive reload.
    before = sends.size();
    activate("weechat.onMessage(e=>weechat.send(e.buffer,'replacement'));");
    assert(managed_hooks().empty() && native_timers.empty());
    dispatch_timer(); assert(sends.size() == before);

    activate("weechat.onMessage(e=>{ try { setInterval(()=>{},100); weechat.send(e.buffer,'added'); } catch(error) { weechat.send(e.buffer,'limit'); } });");
    for (int i = 0; i < 8; ++i) message_event();
    assert(plugins.at("example")->timers.size() == 8 && managed_hooks().size() == 8);
    message_event(); assert(sends.back().second == "limit" && plugins.at("example")->timers.size() == 8);

    for (const char *delay : {"0", "99", "100.5", "NaN", "Infinity", "2147483648", "'100'", "undefined"}) {
        std::string source = "setTimeout(()=>{}," + std::string(delay) + "); weechat.onMessage(()=>{});";
        activate(source.c_str(), false);
    }
    activate("setTimeout('not a function',100); weechat.onMessage(()=>{});", false);
    activate("setTimeout(()=>{},100,'extra'); weechat.onMessage(()=>{});", false);
    activate("setTimeout(()=>{},2147483647); weechat.onMessage(()=>{});");
    assert(fake_timers.at(managed_hooks().front())->interval == 2147483647L);
    auto foreign_id = plugins.at("example")->timers.begin()->first;
    next_candidate = Clock::time_point{};
    std::string source = "weechat.onMessage(()=>{clearTimeout('" + foreign_id + "');clearInterval('unknown');});";
    result = call("javascript_managed_activate", {{"name", "other"}, {"source", source}, {"digest", std::string(64, 'b')}});
    assert(result["ok"] == "1");
    plugins.at("other")->enqueue({"message", "irc.test.#allowed", "alice", "clear", ""});
    plugins.at("other")->turn();
    assert(plugins.at("example")->timers.contains(foreign_id));
    plugins.erase("other");

    activate("setTimeout(()=>weechat.send('irc.test.#allowed','queue-once'),100); weechat.onMessage(()=>{});");
    auto owner = plugins.at("example");
    for (size_t i = 0; i < QUEUE_LIMIT; ++i)
        owner->enqueue({"message", "irc.test.#outside", "alice", "ignored", ""});
    fire_timer(managed_hooks().front());
    assert(owner->jobs.size() == QUEUE_LIMIT && owner->timers.begin()->second.due);
    assert(!owner->timers.begin()->second.queued);
    before = sends.size();
    for (size_t i = 0; i <= QUEUE_LIMIT; ++i) dispatch_timer();
    assert(sends.size() == before + 1 && sends.back().second == "queue-once" && owner->timers.empty());
    owner.reset();

    activate("setTimeout(()=>weechat.send('irc.test.#allowed','evicted-once'),100); weechat.onMessage(()=>weechat.http('GET','https://example.com/','',()=>{}));");
    message_event();
    owner = plugins.at("example");
    for (size_t i = 0; i < QUEUE_LIMIT - 1; ++i)
        owner->enqueue({"message", "irc.test.#outside", "alice", "ignored", ""});
    fire_timer(managed_hooks().front());
    assert(owner->jobs.size() == QUEUE_LIMIT && owner->jobs.back().kind == "timer");
    owner->completion(requests.back(), "{}"); // Priority HTTP completion evicts the queued timer.
    assert(owner->jobs.size() == QUEUE_LIMIT && !owner->timers.begin()->second.queued);
    before = sends.size();
    for (size_t i = 0; i <= QUEUE_LIMIT; ++i) dispatch_timer();
    assert(sends.size() == before + 1 && sends.back().second == "evicted-once" && owner->timers.empty());
    owner.reset();
    requests.clear(); // Timer fixture HTTP request is independent of the existing suite.

    activate("setInterval(()=>{setTimeout(()=>{},100); while(true){}},100); weechat.onMessage(()=>{});");
    fire_timer(managed_hooks().front());
    auto start = Clock::now(); dispatch_timer();
    assert(Clock::now() - start < std::chrono::seconds(2));
    assert(!plugins.at("example")->active && plugins.at("example")->timers.empty());
    dispatch_timer(); assert(managed_hooks().empty() && native_timers.empty());
    activate("weechat.onMessage(()=>{});");
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
    // Exercise status-only timer proof without granting any script effects.
    std::filesystem::remove(root / "data.json");
    readonly(root / "data.json", R"({"managed":{"buffers":[],"http_methods":[],"http_hosts":[],"log":false}})");
    assert(weechat_js_managed_init());
    timer_observability_tests();
    weechat_js_managed_end();
    std::filesystem::remove(root / "data.json");
    readonly(root / "data.json", R"({"managed":{"buffers":["irc.test.#allowed"],"http_methods":["GET"],"http_hosts":["example.com"],"log":true}})");
    assert(weechat_js_managed_init());
    timer_tests();
    sends.clear();
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
    activate("setInterval(()=>{},100); weechat.onMessage(e => { if(e.text === 'first') weechat.http('GET','https://example.com/','',()=>{}); else while(true){} });");
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
    activate("setInterval(()=>{},100); weechat.onMessage(e => { if(e.text === 'first') weechat.http('GET','https://example.com/','',()=>{}); else { weechat.send(e.buffer,'before-unload'); weechat.send(e.buffer,'must-not-send'); } });");
    message_event("first");
    size_t send_count = sends.size();
    unload_on_send = true;
    message_event("unload"); // Clears the entire fake plugin vtable while draining.
    assert(!enabled && plugins.empty() && sends.size() == send_count + 1);
    assert(managed_hooks().empty() && native_timers.empty());
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

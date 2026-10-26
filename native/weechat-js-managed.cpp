// SPDX-License-Identifier: GPL-3.0-or-later
// An additive, host-classified lane in WeeChat's existing JavaScript plugin.
// Legacy trusted contexts keep their original bindings and execution behavior.
#include <v8.h>
#include <libplatform/libplatform.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <dirent.h>
#include <fcntl.h>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>
extern "C" {
#include "../weechat-plugin.h"
#include "weechat_managed_policy.h"
}
#include "weechat-js.h"
#include "weechat-js-v8.h"
#include "weechat-js-managed.h"

extern v8::Platform *js_platform;

namespace {
using namespace v8;
using Clock = std::chrono::steady_clock;
constexpr size_t SOURCE_LIMIT = 65536;
constexpr size_t EFFECT_LIMIT = 4;
constexpr size_t PLUGIN_LIMIT = 8;
constexpr size_t QUEUE_LIMIT = 32;
constexpr size_t HTTP_LIMIT = 2;
constexpr int DEADLINE_MS = 50;
std::string managed_root;
int managed_root_fd = -1;
weechat_managed_policy *policy = nullptr;
bool enabled = false;
bool ticking = false;
uint64_t next_generation = 0;
uint64_t next_request = 0;
Clock::time_point next_candidate;
std::deque<Clock::time_point> send_times;
std::deque<Clock::time_point> log_times;
std::deque<std::string> cancellations;

std::string quote(const std::string &value) {
    std::string result = "\"";
    const char *hex = "0123456789abcdef";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { result += '\\'; result += static_cast<char>(c); }
        else if (c < 32) { result += "\\u00"; result += hex[c >> 4]; result += hex[c & 15]; }
        else result += static_cast<char>(c);
    }
    return result + "\"";
}

bool valid_name(const std::string &value) {
    if (value.empty() || value.size() > 48) return false;
    unsigned char first = value.front();
    if (!((first >= 'a' && first <= 'z') || (first >= 'A' && first <= 'Z') || (first >= '0' && first <= '9'))) return false;
    for (unsigned char c : value)
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
              (c >= '0' && c <= '9') || c == '_' || c == '-')) return false;
    return true;
}

bool single_line(const std::string &value, size_t limit) {
    if (value.size() > limit) return false;
    for (unsigned char c : value) if (c < 32 || c == 127) return false;
    return true;
}

std::string canonical(const char *path) {
    if (!path || path[0] != '/') throw std::runtime_error("absolute_path_required");
    char *resolved = realpath(path, nullptr);
    if (!resolved) throw std::runtime_error("path_unavailable");
    std::string result(resolved);
    free(resolved);
    return result;
}

bool beneath(const std::string &path) {
    // The directory descriptor pins authority even if its operator renames the
    // directory after startup. Never let relocation promote the same source.
    std::string current = managed_root;
    if (managed_root_fd >= 0) {
        try { current = canonical(("/proc/self/fd/" + std::to_string(managed_root_fd)).c_str()); }
        catch (...) { return true; } // Lost root identity is ambiguous authority.
    }
    return path == current || path.starts_with(current + "/");
}

bool shared_managed_inode(int directory, dev_t device, ino_t inode, size_t &remaining, unsigned depth = 0) {
    if (depth > 8) throw std::runtime_error("managed_tree_too_deep");
    int scan_fd = openat(directory, ".", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (scan_fd < 0) throw std::runtime_error("managed_tree_unavailable");
    DIR *raw = fdopendir(scan_fd);
    if (!raw) { close(scan_fd); throw std::runtime_error("managed_tree_unavailable"); }
    std::unique_ptr<DIR, int (*)(DIR *)> scan(raw, &closedir);
    while (dirent *entry = readdir(scan.get())) {
        if (strcmp(entry->d_name, ".") == 0 || strcmp(entry->d_name, "..") == 0) continue;
        if (remaining-- == 0) throw std::runtime_error("managed_tree_too_large");
        struct stat info{};
        if (fstatat(directory, entry->d_name, &info, AT_SYMLINK_NOFOLLOW) != 0)
            throw std::runtime_error("managed_tree_changed");
        if (S_ISLNK(info.st_mode)) {
            // A managed writable alias must not promote its target to trusted.
            if (fstatat(directory, entry->d_name, &info, 0) != 0) continue;
        }
        if (info.st_dev == device && info.st_ino == inode) return true;
        if (S_ISDIR(info.st_mode)) {
            int child = openat(directory, entry->d_name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
            if (child < 0) throw std::runtime_error("managed_tree_changed");
            bool found;
            try { found = shared_managed_inode(child, device, inode, remaining, depth + 1); }
            catch (...) { close(child); throw; }
            close(child);
            if (found) return true;
        }
    }
    return false;
}

std::string read_policy(const char *path) {
    std::string resolved = canonical(path);
    if (beneath(resolved)) throw std::runtime_error("policy_inside_managed_root");
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) throw std::runtime_error("policy_unavailable");
    struct stat info{};
    if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) ||
        (info.st_uid != 0 && info.st_uid != getuid()) || (info.st_mode & 0222) ||
        info.st_size <= 0 || info.st_size > static_cast<off_t>(SOURCE_LIMIT)) {
        close(fd); throw std::runtime_error("policy_not_readonly_regular");
    }
    std::string value(static_cast<size_t>(info.st_size), '\0');
    size_t offset = 0;
    while (offset < value.size()) {
        ssize_t count = read(fd, value.data() + offset, value.size() - offset);
        if (count <= 0) { close(fd); throw std::runtime_error("policy_read_failed"); }
        offset += static_cast<size_t>(count);
    }
    char extra;
    bool changed = read(fd, &extra, 1) != 0;
    close(fd);
    if (changed || value.find('\0') != std::string::npos) throw std::runtime_error("policy_invalid");
    return value;
}

std::string field(t_hashtable *table, const char *key) {
    const char *value = static_cast<const char *>(weechat_hashtable_get(table, key));
    return value ? value : "";
}

t_hashtable *table(const std::map<std::string, std::string> &values) {
    auto *result = weechat_hashtable_new(16, WEECHAT_HASHTABLE_STRING, WEECHAT_HASHTABLE_STRING, nullptr, nullptr);
    if (!result) throw std::runtime_error("allocation_failed");
    for (const auto &[key, value] : values) weechat_hashtable_set(result, key.c_str(), value.c_str());
    return result;
}

// This function must only be called after the native watchdog has joined.
std::map<std::string, std::string> host(const char *name,
                                      const std::map<std::string, std::string> &values) {
    if (!enabled) return {};
    auto *input = table(values);
    // A trusted callback can unload this plugin synchronously. Core hashtable
    // functions do not take a plugin pointer; snapshot them BEFORE reentry.
    auto get = weechat_js_plugin->hashtable_get;
    auto release = weechat_js_plugin->hashtable_free;
    auto invoke = weechat_js_plugin->info_get_hashtable;
    auto *owner = weechat_js_plugin;
    auto *output = invoke(owner, name, input);
    release(input);
    std::map<std::string, std::string> result;
    if (output) {
        const char *keys[] = {"ok", "error", "method", "url", "scheme", "host", "port", "body", "accepted"};
        for (const char *key : keys) {
            const char *value = static_cast<const char *>(get(output, key));
            result[key] = value ? value : "";
        }
        release(output);
    }
    return result;
}

class Deadline {
    std::mutex mutex;
    std::condition_variable condition;
    bool done = false;
    std::thread watcher;
public:
    explicit Deadline(Isolate *isolate) : watcher([this, isolate, until = Clock::now() + std::chrono::milliseconds(DEADLINE_MS)] {
        std::unique_lock<std::mutex> lock(mutex);
        if (!condition.wait_until(lock, until, [this] { return done; }))
            isolate->TerminateExecution();
    }) {}
    ~Deadline() {
        { std::lock_guard<std::mutex> lock(mutex); done = true; }
        condition.notify_one();
        watcher.join();
    }
};

struct Effect {
    std::string operation, buffer, text, method, url, body, id;
};
struct Job {
    std::string kind, buffer, nick, text, id;
};
struct Pending {
    Global<Function> callback;
    std::string buffer;
    bool completed = false;
};

class Managed {
    ArrayBuffer::Allocator *allocator = nullptr;
    Isolate *isolate = nullptr;
    Global<Context> context;
    Global<Function> message;
    bool staging = true;
    bool evaluating = false;
    std::string current_buffer;
    std::vector<Effect> effects;
    std::deque<Clock::time_point> invocations;
public:
    const std::string name;
    const std::string digest;
    const uint64_t generation;
    bool active = true;
    std::deque<Job> jobs;
    std::map<std::string, Pending> pending;

    Managed(std::string id, std::string revision) : name(std::move(id)), digest(std::move(revision)), generation(++next_generation) {
        // Reuse the existing plugin's single-threaded V8 platform, not Node.js.
        // Its legacy isolate is a different object and is never terminated here.
        weechat_js_v8_init();
        allocator = ArrayBuffer::Allocator::NewDefaultAllocator();
        Isolate::CreateParams params;
        params.array_buffer_allocator = allocator;
        isolate = Isolate::New(params);
        if (!isolate) { delete allocator; throw std::runtime_error("isolate_failed"); }
        isolate->SetMicrotasksPolicy(MicrotasksPolicy::kExplicit);
        Isolate::Scope isolate_scope(isolate);
        HandleScope handles(isolate);
        auto global = ObjectTemplate::New(isolate);
        auto api = ObjectTemplate::New(isolate);
        const std::pair<const char *, FunctionCallback> bindings[] = {
            {"onMessage", on_message}, {"send", send}, {"http", http}, {"log", log}
        };
        for (const auto &[key, callback] : bindings)
            api->Set(isolate, key, FunctionTemplate::New(isolate, callback, External::New(isolate, this)),
                     static_cast<PropertyAttribute>(ReadOnly | DontDelete));
        global->Set(isolate, "weechat", api, static_cast<PropertyAttribute>(ReadOnly | DontDelete));
        auto ctx = Context::New(isolate, nullptr, global);
        context.Reset(isolate, ctx);
        Context::Scope scope(ctx);
        // No shared-memory wait primitive or GC callback schedule is exposed.
        for (const char *key : {"SharedArrayBuffer", "Atomics", "WebAssembly", "FinalizationRegistry", "WeakRef"})
            if (!ctx->Global()->Delete(ctx, String::NewFromUtf8(isolate, key).ToLocalChecked()).FromMaybe(false))
                throw std::runtime_error("context_initialization_failed");
    }
    ~Managed() {
        // Never call the host from a destructor: it may run after synchronous
        // plugin unload has removed the core plugin object.
        retire();
        message.Reset();
        context.Reset();
        platform::NotifyIsolateShutdown(js_platform, isolate);
        isolate->Dispose();
        delete allocator;
    }

    void retire() {
        active = false;
        jobs.clear();
        effects.clear();
        for (auto &[id, request] : pending) {
            if (enabled && cancellations.size() < 64) cancellations.push_back(id);
            request.callback.Reset();
        }
        pending.clear();
    }

    bool authorize(const std::string &operation, const std::string &buffer, const std::string &request) const {
        if (staging || !active || !policy) return false;
        std::string input = "{\"principal\":{\"id\":" + quote(name) + ",\"generation\":" +
            std::to_string(generation) + ",\"trust\":\"managed\"},\"phase\":\"active\",\"operation\":" +
            quote(operation) + ",\"resource\":{\"buffer\":" + quote(buffer) + "},\"request\":" + request + "}";
        return weechat_managed_policy_authorize(policy,
            reinterpret_cast<const uint8_t *>(input.data()), input.size()) == WEECHAT_MANAGED_POLICY_ALLOW;
    }

    bool evaluate(const std::function<bool(Local<Context>)> &operation) {
        if (!active || evaluating) return false;
        Isolate::Scope isolate_scope(isolate);
        HandleScope handles(isolate);
        auto ctx = context.Get(isolate);
        Context::Scope scope(ctx);
        TryCatch error(isolate);
        evaluating = true;
        bool ok = false;
        try {
            // Every host binding queues intent only. No WeeChat API, foreign
            // callback or fork is permitted until this RAII watchdog is joined.
            Deadline deadline(isolate);
            ok = operation(ctx);
            if (ok) {
                isolate->PerformMicrotaskCheckpoint();
                // Pump only this isolate, never the legacy trusted task queue,
                // under the same deadline and with a bounded task count.
                for (int task = 0; task < 4 && !isolate->IsExecutionTerminating(); ++task)
                    if (!platform::PumpMessageLoop(js_platform, isolate)) break;
            }
        } catch (...) { ok = false; }
        evaluating = false;
        if (error.HasTerminated() || isolate->IsExecutionTerminating()) {
            isolate->CancelTerminateExecution();
            active = false;
            ok = false;
        }
        if (error.HasCaught()) ok = false;
        if (!ok) {
            for (const auto &effect : effects) {
                if (effect.operation == "http.request") pending.erase(effect.id);
            }
            effects.clear();
        }
        return ok;
    }

    bool load(const std::string &source) {
        bool ok = evaluate([&](Local<Context> ctx) {
            Local<String> text;
            Local<Script> script;
            Local<Value> result;
            return String::NewFromUtf8(isolate, source.data(), NewStringType::kNormal, static_cast<int>(source.size())).ToLocal(&text) &&
                   Script::Compile(ctx, text).ToLocal(&script) && script->Run(ctx).ToLocal(&result);
        });
        if (!ok || message.IsEmpty()) return false;
        staging = false;
        return true;
    }

    void enqueue(const Job &job) {
        if (active && jobs.size() < QUEUE_LIMIT) jobs.push_back(job);
    }

    void turn() {
        if (!active || jobs.empty()) return;
        auto now = Clock::now();
        while (!invocations.empty() && now - invocations.front() >= std::chrono::seconds(1)) invocations.pop_front();
        if (invocations.size() >= 20) return;
        Job job = std::move(jobs.front()); jobs.pop_front();
        if (job.kind == "message" && !authorize("event.receive", job.buffer,
            "{\"nick\":" + quote(job.nick) + ",\"text\":" + quote(job.text) + "}")) return;
        invocations.push_back(now);
        current_buffer = job.buffer;
        bool ok = evaluate([&](Local<Context> ctx) {
            Local<Function> callback;
            Local<Value> argument;
            if (job.kind == "message") {
                callback = message.Get(isolate);
                auto value = Object::New(isolate);
                for (const auto &[key, text] : std::map<std::string, std::string>{{"buffer", job.buffer}, {"nick", job.nick}, {"text", job.text}})
                    if (!value->Set(ctx, String::NewFromUtf8(isolate, key.c_str()).ToLocalChecked(), String::NewFromUtf8(isolate, text.c_str()).ToLocalChecked()).FromMaybe(false)) return false;
                argument = value;
            } else {
                auto found = pending.find(job.id);
                if (found == pending.end()) return true;
                callback = found->second.callback.Get(isolate);
                found->second.callback.Reset(); pending.erase(found);
                Local<String> text;
                if (!String::NewFromUtf8(isolate, job.text.data(), NewStringType::kNormal, static_cast<int>(job.text.size())).ToLocal(&text) ||
                    !JSON::Parse(ctx, text).ToLocal(&argument)) return false;
            }
            Local<Value> result;
            return callback->Call(ctx, Undefined(isolate), 1, &argument).ToLocal(&result);
        });
        current_buffer.clear();
        if (ok) drain();
        else if (!active) {
            retire();
            weechat_printf(nullptr, "javascript: managed plugin %s disabled after deadline", name.c_str());
        }
    }

    void completion(const std::string &id, const std::string &json) {
        auto found = pending.find(id);
        if (found != pending.end() && !found->second.completed && active) {
            found->second.completed = true;
            if (jobs.size() >= QUEUE_LIMIT) jobs.pop_back();
            jobs.push_front({"http", found->second.buffer, "", json, id});
        }
    }

private:
    static Managed *identity(const FunctionCallbackInfo<Value> &args) {
        if (!args.Data()->IsExternal()) return nullptr;
        auto *self = static_cast<Managed *>(args.Data().As<External>()->Value());
        if (!self || self->isolate != args.GetIsolate() || !self->evaluating || !self->active) return nullptr;
        return self;
    }
    static void fail(const FunctionCallbackInfo<Value> &args, const char *code) {
        args.GetIsolate()->ThrowException(Exception::Error(String::NewFromUtf8(args.GetIsolate(), code).ToLocalChecked()));
    }
    static bool text(const FunctionCallbackInfo<Value> &args, int index, std::string &out, size_t maximum) {
        if (index >= args.Length() || !args[index]->IsString() ||
            static_cast<size_t>(args[index].As<String>()->Length()) > maximum) return false;
        String::Utf8Value value(args.GetIsolate(), args[index]);
        if (!*value || value.length() < 0 || static_cast<size_t>(value.length()) > maximum) return false;
        out.assign(*value, static_cast<size_t>(value.length()));
        return out.find('\0') == std::string::npos;
    }
    static bool effects_ok(Managed *self) {
        return self && !self->staging && self->effects.size() < EFFECT_LIMIT;
    }
    static void on_message(const FunctionCallbackInfo<Value> &args) {
        auto *self = identity(args);
        if (!self || !self->staging || !self->message.IsEmpty() || args.Length() != 1 || !args[0]->IsFunction()) {
            fail(args, "onMessage_requires_one_staging_callback"); return;
        }
        self->message.Reset(args.GetIsolate(), args[0].As<Function>());
    }
    static void send(const FunctionCallbackInfo<Value> &args) {
        auto *self = identity(args);
        Effect value; value.operation = "irc.send";
        if (!effects_ok(self) || args.Length() != 2 || !text(args, 0, value.buffer, 256) ||
            !text(args, 1, value.text, 350) || !single_line(value.text, 350)) { fail(args, "invalid_send_or_effect_limit"); return; }
        self->effects.push_back(std::move(value));
        args.GetReturnValue().Set(String::NewFromUtf8Literal(args.GetIsolate(), "queued"));
    }
    static void log(const FunctionCallbackInfo<Value> &args) {
        auto *self = identity(args);
        Effect value; value.operation = "log";
        if (!effects_ok(self) || args.Length() != 1 || !text(args, 0, value.text, 512) ||
            !single_line(value.text, 512)) { fail(args, "invalid_log_or_effect_limit"); return; }
        value.buffer = self->current_buffer;
        self->effects.push_back(std::move(value));
    }
    static void http(const FunctionCallbackInfo<Value> &args) {
        auto *self = identity(args);
        Effect value; value.operation = "http.request";
        if (!effects_ok(self) || self->pending.size() >= HTTP_LIMIT || args.Length() != 4 ||
            !text(args, 0, value.method, 6) || !text(args, 1, value.url, 2048) || !text(args, 2, value.body, 16384) || !args[3]->IsFunction() ||
            (value.method != "GET" && value.method != "POST" && value.method != "PUT" && value.method != "DELETE")) {
            fail(args, "invalid_http_or_request_limit"); return;
        }
        value.id = std::to_string(++next_request);
        value.buffer = self->current_buffer;
        Pending pending;
        pending.callback.Reset(args.GetIsolate(), args[3].As<Function>());
        pending.buffer = value.buffer;
        self->pending.emplace(value.id, std::move(pending));
        args.GetReturnValue().Set(String::NewFromUtf8(args.GetIsolate(), value.id.c_str()).ToLocalChecked());
        self->effects.push_back(std::move(value));
    }
    void denied(const Effect &effect, const char *code) {
        if (effect.operation == "http.request")
            completion(effect.id, "{\"id\":" + quote(effect.id) + ",\"error\":" + quote(code) + ",\"outcome\":\"not_sent\"}");
    }
    void drain() {
        std::vector<Effect> work; work.swap(effects);
        for (const auto &effect : work) {
            if (!enabled || !active) break;
            if (effect.operation == "http.request") {
                auto values = host("mcp_managed_http_validate", {{"method", effect.method}, {"url", effect.url}, {"body", effect.body}});
                if (values["ok"] != "1") { denied(effect, "invalid_http_request"); continue; }
                // The trusted adapter must return exact method/body and a
                // canonical URL; authority still derives only from this context.
                if (values["method"] != effect.method || values["body"] != effect.body || values["url"].size() > 2048 ||
                    values["host"].size() > 253 || (values["scheme"] != "http" && values["scheme"] != "https") ||
                    values["port"].empty() || values["port"].find_first_not_of("0123456789") != std::string::npos || values["port"].size() > 5) {
                    denied(effect, "invalid_http_normalization"); continue;
                }
                std::string request = "{\"method\":" + quote(values["method"]) + ",\"url\":" + quote(values["url"]) +
                    ",\"scheme\":" + quote(values["scheme"]) + ",\"host\":" + quote(values["host"]) + ",\"port\":" + values["port"] + ",\"body\":" + quote(values["body"]) + "}";
                if (!authorize(effect.operation, effect.buffer, request)) { denied(effect, "policy_denied"); continue; }
                auto result = host("mcp_managed_http_start", {{"id", effect.id}, {"method", values["method"]}, {"url", values["url"]}, {"body", values["body"]}});
                if (result["ok"] != "1") denied(effect, "http_start_failed");
            } else if (authorize(effect.operation, effect.buffer, "{\"text\":" + quote(effect.text) + "}")) {
                if (effect.operation == "log") {
                    auto now = Clock::now();
                    while (!log_times.empty() && now - log_times.front() >= std::chrono::seconds(1)) log_times.pop_front();
                    if (log_times.size() < 4) {
                        log_times.push_back(now);
                        weechat_printf(nullptr, "javascript managed %s: %s", name.c_str(), effect.text.c_str());
                    }
                }
                else {
                    auto now = Clock::now();
                    while (!send_times.empty() && now - send_times.front() >= std::chrono::seconds(1)) send_times.pop_front();
                    if (send_times.size() < 4) {
                        send_times.push_back(now);
                        (void)host("mcp_managed_send", {{"buffer", effect.buffer}, {"text", effect.text}});
                    }
                }
            }
        }
    }
};

std::map<std::string, std::shared_ptr<Managed>> plugins;

t_hashtable *manager(const void *, void *, const char *operation, t_hashtable *input) {
    try {
        if (!enabled || !policy) return table({{"error", "managed_unavailable"}});
        std::string op = operation ? operation : "";
        if (op == "javascript_managed_status") {
            std::string json = "[";
            for (const auto &[name, plugin] : plugins) {
                if (json.size() > 1) json += ',';
                json += "{\"name\":" + quote(name) + ",\"digest\":" + quote(plugin->digest) +
                    ",\"generation\":" + std::to_string(plugin->generation) + ",\"active\":" + (plugin->active ? "true" : "false") + "}";
            }
            return table({{"ok", "1"}, {"plugins", json + "]"}});
        }
        if (op == "javascript_managed_http_complete") {
            std::string id = field(input, "id"), response = field(input, "response_json");
            if (id.size() > 24 || response.size() > 512 * 1024) return table({{"error", "invalid_completion"}});
            for (auto &[name, plugin] : plugins) { (void)name; plugin->completion(id, response); }
            return table({{"ok", "1"}});
        }
        if (op == "javascript_managed_message") {
            std::string buffer = field(input, "buffer"), nick = field(input, "nick"), text = field(input, "text");
            if (buffer.size() > 256 || nick.size() > 128 || text.size() > 4096) return table({{"error", "message_too_large"}});
            for (auto &[name, plugin] : plugins) { (void)name; plugin->enqueue({"message", buffer, nick, text, ""}); }
            return table({{"ok", "1"}});
        }
        if (op != "javascript_managed_validate" && op != "javascript_managed_activate") return table({{"error", "unknown_operation"}});
        if (ticking) return table({{"error", "managed_busy"}});
        std::string name = field(input, "name"), source = field(input, "source"), digest = field(input, "digest");
        if (!valid_name(name) || source.empty() || source.size() > SOURCE_LIMIT || digest.size() != 64 ||
            digest.find_first_not_of("0123456789abcdef") != std::string::npos) return table({{"error", "invalid_source"}});
        if (plugins.find(name) == plugins.end() && plugins.size() >= PLUGIN_LIMIT) return table({{"error", "plugin_limit"}});
        // MCP may pipeline many synchronous requests in one event-loop tick.
        // Admit at most one candidate evaluation per 250ms globally, rather
        // than multiplying the 50ms V8 window across clients/request batches.
        auto now = Clock::now();
        if (now < next_candidate)
            return table({{"error", "candidate_rate_limited"}, {"retry_after_ms", std::to_string(std::chrono::duration_cast<std::chrono::milliseconds>(next_candidate - now).count() + 1)}});
        next_candidate = now + std::chrono::milliseconds(250);
        auto candidate = std::make_shared<Managed>(name, digest);
        if (!candidate->load(source)) return table({{"error", "candidate_rejected"}});
        std::string generation = std::to_string(candidate->generation);
        if (op == "javascript_managed_activate") {
            auto found = plugins.find(name);
            std::shared_ptr<Managed> previous;
            if (found != plugins.end()) { previous = std::move(found->second); plugins.erase(found); }
            plugins.emplace(name, std::move(candidate));
            previous.reset();
        }
        return table({{"ok", "1"}, {"digest", digest}, {"generation", generation}});
    } catch (...) { return table({{"error", "managed_failure"}}); }
}

int timer(const void *, void *, int) {
    if (ticking) return WEECHAT_RC_OK;
    ticking = true;
    try {
        // Deferred cancellations cannot re-enter object destruction/activation.
        for (int count = 0; enabled && !cancellations.empty() && count < 4; ++count) {
            std::string id = std::move(cancellations.front());
            cancellations.pop_front();
            (void)host("mcp_managed_http_cancel", {{"id", id}});
        }
        // One managed turn per tick, round-robin: eight malicious contexts
        // must not multiply the event-loop pause to eight deadline windows.
        // The next trusted WeeChat iteration runs between managed turns.
        static std::string previous;
        if (enabled && !plugins.empty()) {
            auto next = plugins.upper_bound(previous);
            if (next == plugins.end()) next = plugins.begin();
            previous = next->first;
            auto current = next->second; // Keep this alive across trusted unload.
            current->turn();
        }
    } catch (...) { if (enabled) weechat_printf(nullptr, "javascript: managed dispatch failed"); }
    ticking = false;
    return WEECHAT_RC_OK;
}
} // namespace

bool weechat_js_managed_path(const char *filename) {
    if (managed_root.empty()) return false;
    try { return beneath(canonical(filename)); }
    catch (...) { return true; } // Configured managed lane: ambiguous path is not trusted.
}

char *weechat_js_managed_read_trusted(const char *filename, bool *handled) {
    *handled = !managed_root.empty();
    if (!*handled) return nullptr; // Opt-out preserves the original reader.
    int fd = filename ? open(filename, O_RDONLY | O_CLOEXEC | O_NONBLOCK) : -1;
    if (fd < 0) return nullptr;
    char *source = nullptr;
    try {
        struct stat info{};
        std::string descriptor = "/proc/self/fd/" + std::to_string(fd);
        // Follow ordinary trusted symlinks, but classify the descriptor's
        // actual inode and then read THAT inode, never re-open the user path.
        if (fstat(fd, &info) != 0 || !S_ISREG(info.st_mode) ||
            beneath(canonical(descriptor.c_str()))) throw std::runtime_error("untrusted_script_path");
        // Nix store deduplication legitimately hardlinks trusted scripts. Deny
        // only an inode shared with the managed tree (or an ambiguous scan),
        // not every multiply linked trusted file.
        if (info.st_nlink > 1) {
            size_t remaining = 1024;
            if (managed_root_fd < 0 || shared_managed_inode(managed_root_fd, info.st_dev, info.st_ino, remaining))
                throw std::runtime_error("managed_inode_alias");
        }
        source = weechat_file_get_content(descriptor.c_str());
    } catch (...) {}
    close(fd);
    return source;
}

bool weechat_js_managed_init() {
    const char *mode = getenv("WEECHAT_MCP_MANAGED_JS");
    if (!mode || strcmp(mode, "0") == 0) return true;
    if (strcmp(mode, "1") != 0) return false;
    try {
        managed_root = canonical(getenv("WEECHAT_MANAGED_ROOT"));
        managed_root_fd = open(managed_root.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
        if (managed_root_fd < 0) throw std::runtime_error("unsafe_managed_root");
        struct stat root{};
        if (fstat(managed_root_fd, &root) != 0 || !S_ISDIR(root.st_mode) || (root.st_mode & 0022)) throw std::runtime_error("unsafe_managed_root");
        std::string source = read_policy(getenv("WEECHAT_MANAGED_POLICY"));
        std::string data = read_policy(getenv("WEECHAT_MANAGED_DATA"));
        policy = weechat_managed_policy_load(reinterpret_cast<const uint8_t *>(source.data()), source.size(), reinterpret_cast<const uint8_t *>(data.data()), data.size());
        if (!policy) throw std::runtime_error("policy_load_failed");
        enabled = true;
        for (const char *name : {"javascript_managed_validate", "javascript_managed_activate", "javascript_managed_status", "javascript_managed_message", "javascript_managed_http_complete"})
            if (!weechat_hook_info_hashtable(name, "Managed JavaScript host interface", "", "", &manager, nullptr, nullptr)) throw std::runtime_error("manager_hook_failed");
        if (!weechat_hook_timer(50, 0, 0, &timer, nullptr, nullptr)) throw std::runtime_error("manager_timer_failed");
        return true;
    } catch (...) {
        weechat_printf(nullptr, "javascript: managed startup failed closed");
        weechat_js_managed_end();
        return false;
    }
}

void weechat_js_managed_end() {
    enabled = false;
    auto removed = std::move(plugins);
    plugins.clear();
    for (auto &[name, plugin] : removed) { (void)name; plugin->retire(); }
    removed.clear();
    cancellations.clear();
    if (policy) { weechat_managed_policy_free(policy); policy = nullptr; }
    if (managed_root_fd >= 0) { close(managed_root_fd); managed_root_fd = -1; }
}

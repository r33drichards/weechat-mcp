// SPDX-License-Identifier: GPL-3.0-or-later
// Feasibility regression: a managed isolate's native deadline must not terminate
// a legacy isolate, and no watchdog thread may survive a return to the host.
#include <v8.h>
#include <libplatform/libplatform.h>
#include <atomic>
#include <cassert>
#include <chrono>
#include <condition_variable>
#include <iostream>
#include <mutex>
#include <thread>
#include <sys/wait.h>
#include <unistd.h>

using namespace v8;
static std::atomic<int> watchdogs{0};
static v8::Platform *active_platform = nullptr;

class Deadline {
    std::mutex mutex;
    std::condition_variable condition;
    bool finished = false;
    std::thread watcher;
public:
    explicit Deadline(Isolate *isolate) : watcher([this, isolate, until = std::chrono::steady_clock::now() + std::chrono::milliseconds(50)] {
        watchdogs.fetch_add(1);
        std::unique_lock<std::mutex> lock(mutex);
        if (!condition.wait_until(lock, until, [this] { return finished; }))
            isolate->TerminateExecution(); // Documented thread-safe V8 API only.
        watchdogs.fetch_sub(1);
    }) {}
    ~Deadline() {
        { std::lock_guard<std::mutex> lock(mutex); finished = true; }
        condition.notify_one();
        watcher.join();
    }
};

class Runtime {
public:
    ArrayBuffer::Allocator *allocator;
    Isolate *isolate;
    Global<Context> context;
    Runtime() {
        allocator = ArrayBuffer::Allocator::NewDefaultAllocator();
        Isolate::CreateParams params;
        params.array_buffer_allocator = allocator;
        isolate = Isolate::New(params);
        isolate->SetMicrotasksPolicy(MicrotasksPolicy::kExplicit);
        Isolate::Scope scope(isolate);
        HandleScope handles(isolate);
        context.Reset(isolate, Context::New(isolate));
    }
    ~Runtime() {
        assert(watchdogs.load() == 0);
        context.Reset();
        platform::NotifyIsolateShutdown(active_platform, isolate);
        isolate->Dispose();
        delete allocator;
    }
    bool eval(const char *source, bool bounded, int *result = nullptr) {
        Isolate::Scope scope(isolate);
        HandleScope handles(isolate);
        auto ctx = context.Get(isolate);
        Context::Scope context_scope(ctx);
        TryCatch error(isolate);
        auto execute = [&] {
            Local<String> text;
            Local<Script> script;
            Local<Value> value;
            if (!String::NewFromUtf8(isolate, source).ToLocal(&text) ||
                !Script::Compile(ctx, text).ToLocal(&script) ||
                !script->Run(ctx).ToLocal(&value))
                return false;
            isolate->PerformMicrotaskCheckpoint();
            if (isolate->IsExecutionTerminating() || error.HasTerminated()) return false;
            if (result) *result = value->Int32Value(ctx).FromMaybe(-1);
            return true;
        };
        bool ok;
        if (bounded) { Deadline deadline(isolate); ok = execute(); }
        else ok = execute();
        assert(watchdogs.load() == 0);
        if (error.HasTerminated() || isolate->IsExecutionTerminating()) {
            isolate->CancelTerminateExecution();
            return false;
        }
        return ok;
    }
};

int main() {
    V8::SetFlagsFromString("--single-threaded");
    auto platform = platform::NewSingleThreadedDefaultPlatform();
    active_platform = platform.get();
    V8::InitializePlatform(active_platform);
    assert(V8::Initialize());
    {
        Runtime trusted;
        // Simulates existing WeeChat's legacy isolate remaining entered.
        Isolate::Scope legacy_scope(trusted.isolate);
        Runtime managed;
        int value = 0;
        assert(trusted.eval("globalThis.counter = 40", false));
        assert(managed.eval("globalThis.counter = 700; 2+3", true, &value) && value == 5);
        auto started = std::chrono::steady_clock::now();
        assert(!managed.eval("while (true) {}", true));
        assert(std::chrono::steady_clock::now() - started < std::chrono::seconds(2));
        assert(trusted.eval("++counter", false, &value) && value == 41);
        assert(!managed.eval("Promise.resolve().then(() => { while (true) {} }); 1", true));
        assert(trusted.eval("++counter", false, &value) && value == 42);
        assert(managed.eval("counter", true, &value) && value == 700);
        // Only after deadline join: native fork helper can still run, then the
        // parent can use both V8 isolates. The child never touches V8 or Rust.
        pid_t child = fork();
        assert(child >= 0);
        if (child == 0) _exit(0);
        int status;
        assert(waitpid(child, &status, 0) == child && status == 0);
        assert(managed.eval("counter + 1", true, &value) && value == 701);
        assert(trusted.eval("counter + 1", false, &value) && value == 43);
    }
    V8::Dispose();
    V8::DisposePlatform();
    std::cout << "PASS: managed deadlines/microtasks isolated; legacy healthy; watchdog joined before fork\n";
}

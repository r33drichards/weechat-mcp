// SPDX-License-Identifier: GPL-3.0-or-later
//! Independent, synchronous Rego policy evaluator behind a deliberately tiny C ABI.
//!
//! No mcp-js code, service, configuration loader or policy-evaluator dependency is
//! used. The native caller constructs identity and canonical effect arguments.
//! This library cannot authenticate a forged native caller or isolate native OOM.

use regorus::{Engine, Value as RegoValue};
use serde_json::Value;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::slice;
use std::str;
use std::sync::Mutex;

const QUERY: &str = "data.weechat.managed.allow";
const PACKAGE: &str = "data.weechat.managed";
pub const MAX_SOURCE_BYTES: usize = 65_536;
pub const MAX_DATA_BYTES: usize = 65_536;
pub const MAX_INPUT_BYTES: usize = 32_768;
const MAX_JSON_DEPTH: usize = 24;
const MAX_JSON_NODES: usize = 4096;
const MAX_JSON_STRING: usize = 16_384;
const OPERATIONS: &[&str] = &["event.receive", "irc.send", "http.request", "log"];

/// Return codes intentionally distinguish errors from explicit denial, but both
/// fail closed. A successful engine evaluation is NOT necessarily authorization.
pub const ERROR: i32 = -1;
pub const DENY: i32 = 0;
pub const ALLOW: i32 = 1;

#[allow(non_camel_case_types)]
pub struct weechat_managed_policy {
    engine: Mutex<Engine>,
}

/// Validate before interpreting JSON. serde_json retains its own recursion limit
/// as a separate parser backstop; this traversal imposes the tighter envelope.
fn bounded_json(value: &Value) -> bool {
    fn visit(value: &Value, depth: usize, remaining: &mut usize) -> bool {
        if depth > MAX_JSON_DEPTH || *remaining == 0 {
            return false;
        }
        *remaining -= 1;
        match value {
            Value::Null | Value::Bool(_) => true,
            // Canonical policy inputs use integer counts/ports/generations, not
            // floating point or arbitrarily large numeric identifiers.
            Value::Number(number) => number.as_u64().is_some() || number.as_i64().is_some(),
            Value::String(text) => text.len() <= MAX_JSON_STRING && !text.contains('\0'),
            Value::Array(values) => values.iter().all(|v| visit(v, depth + 1, remaining)),
            Value::Object(values) => values.iter().all(|(key, v)| {
                key.len() <= 128 && !key.contains('\0') && visit(v, depth + 1, remaining)
            }),
        }
    }
    let mut remaining = MAX_JSON_NODES;
    visit(value, 0, &mut remaining)
}

fn parse_json(bytes: &[u8], limit: usize) -> Result<Value, ()> {
    if bytes.is_empty() || bytes.len() > limit {
        return Err(());
    }
    let value: Value = serde_json::from_slice(bytes).map_err(|_| ())?;
    if !bounded_json(&value) {
        return Err(());
    }
    Ok(value)
}

fn exact_keys(value: &Value, keys: &[&str]) -> bool {
    value.as_object().is_some_and(|object| {
        object.len() == keys.len() && keys.iter().all(|key| object.contains_key(*key))
    })
}

fn input_envelope(input: &Value) -> Result<bool, ()> {
    if !exact_keys(
        input,
        &["principal", "phase", "operation", "resource", "request"],
    ) || !exact_keys(&input["principal"], &["id", "generation", "trust"])
        || !input["resource"].is_object()
        || !input["request"].is_object()
    {
        return Err(());
    }
    let principal = &input["principal"];
    let Some(id) = principal["id"].as_str() else {
        return Err(());
    };
    if id.is_empty()
        || id.len() > 64
        || !id.as_bytes()[0].is_ascii_alphanumeric()
        || !id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte))
        || principal["generation"]
            .as_u64()
            .is_none_or(|generation| generation == 0)
    {
        return Err(());
    }
    if principal["trust"] != "managed" || input["phase"] != "active" {
        return Ok(false);
    }
    let Some(operation) = input["operation"].as_str() else {
        return Err(());
    };
    // A permissive policy cannot create a native capability the host did not
    // implement. New operations require a reviewed ABI/schema/policy change.
    Ok(OPERATIONS.contains(&operation))
}

fn load(source: &[u8], data: &[u8]) -> Result<weechat_managed_policy, ()> {
    if source.is_empty() || source.len() > MAX_SOURCE_BYTES {
        return Err(());
    }
    let source = str::from_utf8(source).map_err(|_| ())?;
    if source.trim().is_empty() || source.contains('\0') {
        return Err(());
    }
    let data = parse_json(data, MAX_DATA_BYTES)?;
    let Some(data_object) = data.as_object() else {
        return Err(());
    };
    // The query must come from policy, not a data document that injects an
    // `allow: true` value at the policy namespace.
    if data_object.contains_key("weechat") {
        return Err(());
    }
    let mut engine = Engine::new();
    engine.set_strict_builtin_errors(true);
    if engine
        .add_policy("operator.rego".into(), source.into())
        .map_err(|_| ())?
        != PACKAGE
    {
        return Err(());
    }
    engine.add_data(RegoValue::from(data)).map_err(|_| ())?;
    engine.set_input(RegoValue::new_object());
    // Prepare/resolve the fixed rule at load, so malformed references and missing
    // decisions fail startup. Undefined or nonboolean decisions still never allow.
    engine.eval_rule(QUERY.into()).map_err(|_| ())?;
    Ok(weechat_managed_policy {
        engine: Mutex::new(engine),
    })
}

impl weechat_managed_policy {
    fn authorize(&self, bytes: &[u8]) -> i32 {
        let input = match parse_json(bytes, MAX_INPUT_BYTES) {
            Ok(input) => input,
            Err(()) => return ERROR,
        };
        match input_envelope(&input) {
            Err(()) => return ERROR,
            Ok(false) => return DENY,
            Ok(true) => {}
        }
        // No native effect/callback runs inside the evaluator. Reentrant/busy or
        // poisoned evaluators fail closed rather than blocking on a lock.
        let Ok(mut engine) = self.engine.try_lock() else {
            return ERROR;
        };
        engine.set_input(RegoValue::from(input));
        let result = engine.eval_rule(QUERY.into());
        engine.set_input(RegoValue::new_object());
        match result {
            Ok(RegoValue::Bool(true)) => ALLOW,
            Ok(_) => DENY,
            Err(_) => ERROR,
        }
    }
}

/// # Safety
/// Non-null pointers must reference readable memory for the given lengths.
unsafe fn bytes<'a>(pointer: *const u8, length: usize, maximum: usize) -> Result<&'a [u8], ()> {
    if pointer.is_null() || length == 0 || length > maximum {
        return Err(());
    }
    // SAFETY: This is the explicit contract of the trusted C caller. Bounds were
    // checked before constructing the slice; arbitrary native pointers are not a
    // supported hostile-input boundary.
    Ok(unsafe { slice::from_raw_parts(pointer, length) })
}

/// Load immutable trusted policy/data. Returns null on parse, rule or panic errors.
///
/// # Safety
/// Input buffers must be readable for the specified lengths for this call.
#[no_mangle]
pub unsafe extern "C" fn weechat_managed_policy_load(
    source: *const u8,
    source_len: usize,
    data_json: *const u8,
    data_len: usize,
) -> *mut weechat_managed_policy {
    catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: The C API documents the required readable buffer contract.
        let source = unsafe { bytes(source, source_len, MAX_SOURCE_BYTES) }.ok()?;
        let data = unsafe { bytes(data_json, data_len, MAX_DATA_BYTES) }.ok()?;
        load(source, data)
            .ok()
            .map(|policy| Box::into_raw(Box::new(policy)))
    }))
    .ok()
    .flatten()
    .unwrap_or(std::ptr::null_mut())
}

/// Evaluate the fixed allow rule. Only ALLOW grants an effect.
///
/// # Safety
/// `policy` must be a live handle from load, not concurrently freed. The input
/// buffer must be readable for `input_len` bytes until this call returns.
#[no_mangle]
pub unsafe extern "C" fn weechat_managed_policy_authorize(
    policy: *const weechat_managed_policy,
    input_json: *const u8,
    input_len: usize,
) -> i32 {
    if policy.is_null() {
        return ERROR;
    }
    catch_unwind(AssertUnwindSafe(|| {
        // SAFETY: The native caller owns the documented handle/buffer lifetimes.
        let Ok(input) = (unsafe { bytes(input_json, input_len, MAX_INPUT_BYTES) }) else {
            return ERROR;
        };
        unsafe { &*policy }.authorize(input)
    }))
    .unwrap_or(ERROR)
}

/// Release an immutable native handle. A null handle is a no-op.
///
/// # Safety
/// A non-null handle must come from load and be freed exactly once, with no
/// concurrent authorization calls. Catching Rust panics does not fix invalid C
/// lifetimes, double-free or allocator aborts.
#[no_mangle]
pub unsafe extern "C" fn weechat_managed_policy_free(policy: *mut weechat_managed_policy) {
    if !policy.is_null() {
        let _ = catch_unwind(AssertUnwindSafe(|| {
            // SAFETY: The native caller transfers its valid unique handle here.
            drop(unsafe { Box::from_raw(policy) });
        }));
    }
}

#[cfg(test)]
mod tests;

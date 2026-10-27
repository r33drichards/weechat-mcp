// SPDX-License-Identifier: GPL-3.0-or-later
use super::*;
use serde_json::json;

fn policy(body: &str) -> weechat_managed_policy {
    load(
        format!("package weechat.managed\n{body}\n").as_bytes(),
        b"{}",
    )
    .unwrap()
}

fn input(operation: &str) -> Value {
    json!({"principal":{"id":"example","generation":1,"trust":"managed"},
           "phase":"active", "operation":operation,
           "resource":{"buffer":"irc.docker.#wchat"},
           "request":{"text":"hello","method":"GET","host":"example.com","url":"https://example.com/"}})
}

fn decide(policy: &weechat_managed_policy, value: &Value) -> i32 {
    policy.authorize(&serde_json::to_vec(value).unwrap())
}

#[test]
fn only_actual_boolean_true_allows() {
    for (rule, expected) in [
        ("allow := true", ALLOW),
        ("allow := false", DENY),
        ("allow := \"true\"", DENY),
        ("allow := 1", DENY),
        ("allow := [true]", DENY),
        ("allow := {\"allow\": true}", DENY),
        ("allow if { false }", DENY),
    ] {
        assert_eq!(decide(&policy(rule), &input("log")), expected, "{rule}");
    }
}

#[test]
fn policy_namespace_rule_empty_and_malformed_fail_at_load() {
    for source in [
        "",
        " \n ",
        "not rego",
        "package other\nallow := true",
        "package weechat.managed\nother := true",
    ] {
        assert!(load(source.as_bytes(), b"{}").is_err(), "{source}");
    }
}

#[test]
fn source_and_data_size_utf8_nul_and_shape_are_bounded() {
    assert!(load(&vec![b' '; MAX_SOURCE_BYTES + 1], b"{}").is_err());
    assert!(load(b"\xff", b"{}").is_err());
    assert!(load(b"package weechat.managed\nallow := true\0", b"{}").is_err());
    let source = b"package weechat.managed\nallow := true";
    for data in [
        b"".as_slice(),
        b"[1]",
        b"null",
        b"invalid",
        b"{\"weechat\":{\"managed\":{\"allow\":true}}}",
    ] {
        assert!(load(source, data).is_err());
    }
    assert!(load(source, &vec![b' '; MAX_DATA_BYTES + 1]).is_err());
}

#[test]
fn unknown_operations_trust_and_staging_deny_even_with_permissive_policy() {
    let p = policy("allow := true");
    for operation in [
        "exec",
        "command",
        "config.set",
        "hdata.update",
        "signal.send",
        "policy.set",
        "",
    ] {
        assert_eq!(decide(&p, &input(operation)), DENY);
    }
    for trust in ["trusted", "root", "", "Managed"] {
        let mut value = input("log");
        value["principal"]["trust"] = json!(trust);
        assert_eq!(decide(&p, &value), DENY);
    }
    let mut value = input("log");
    value["phase"] = json!("staging");
    assert_eq!(decide(&p, &value), DENY);
}

#[test]
fn malformed_envelopes_and_authority_injection_fail_closed() {
    let p = policy("allow := true");
    for value in [json!(null), json!([]), json!(true), json!({})] {
        assert_eq!(decide(&p, &value), ERROR);
    }
    for key in ["data", "policy", "query", "trusted"] {
        let mut value = input("log");
        value[key] = json!(true);
        assert_eq!(decide(&p, &value), ERROR);
    }
    for id in ["", "../trusted", ".hidden", "a/b", "with space"] {
        let mut value = input("log");
        value["principal"]["id"] = json!(id);
        assert_eq!(decide(&p, &value), ERROR);
    }
    for generation in [json!(0), json!(-1), json!("1"), json!(1.5)] {
        let mut value = input("log");
        value["principal"]["generation"] = generation;
        assert_eq!(decide(&p, &value), ERROR);
    }
}

#[test]
fn input_bytes_nodes_depth_and_strings_are_bounded() {
    let p = policy("allow := true");
    assert_eq!(p.authorize(b""), ERROR);
    assert_eq!(p.authorize(b"\xff"), ERROR);
    assert_eq!(p.authorize(&vec![b' '; MAX_INPUT_BYTES + 1]), ERROR);
    let mut value = input("log");
    value["request"]["text"] = json!("x".repeat(MAX_JSON_STRING + 1));
    assert_eq!(decide(&p, &value), ERROR);
    value = input("log");
    value["request"]["text"] = json!("x\0y");
    assert_eq!(decide(&p, &value), ERROR);
    value = input("log");
    value["request"]["list"] = json!(vec![true; MAX_JSON_NODES]);
    assert_eq!(decide(&p, &value), ERROR);
    let mut deep = json!(null);
    for _ in 0..MAX_JSON_DEPTH {
        deep = json!([deep]);
    }
    value = input("log");
    value["request"]["deep"] = deep;
    assert_eq!(decide(&p, &value), ERROR);
}

#[test]
fn input_dependent_builtin_error_denies_and_does_not_leave_input() {
    let p = policy("default allow := false\nallow if { input.request.break; x := 1 / 0; x == 1 }");
    let mut value = input("log");
    value["request"]["break"] = json!(true);
    assert_eq!(decide(&p, &value), ERROR);
    assert_eq!(decide(&p, &input("log")), DENY);
}

#[test]
fn handles_have_independent_immutable_policy_data() {
    let source = b"package weechat.managed\nallow := data.granted";
    let a = load(source, br#"{"granted":true}"#).unwrap();
    let b = load(source, br#"{"granted":false}"#).unwrap();
    assert_eq!(decide(&a, &input("log")), ALLOW);
    assert_eq!(decide(&b, &input("log")), DENY);
    assert_eq!(decide(&a, &input("log")), ALLOW);
}

#[test]
fn busy_and_poisoned_engines_fail_closed() {
    let p = policy("allow := true");
    let guard = p.engine.lock().unwrap();
    assert_eq!(decide(&p, &input("log")), ERROR);
    drop(guard);
    assert_eq!(decide(&p, &input("log")), ALLOW);
    let _ = catch_unwind(AssertUnwindSafe(|| {
        let _guard = p.engine.lock().unwrap();
        panic!("simulate evaluator panic");
    }));
    assert_eq!(decide(&p, &input("log")), ERROR);
}

#[test]
fn sample_policy_applies_uniform_lane_buffers_http_and_log() {
    let p = load(
        include_bytes!("../managed.rego"),
        include_bytes!("../example-data.json"),
    )
    .unwrap();
    for operation in OPERATIONS {
        assert_eq!(decide(&p, &input(operation)), ALLOW);
    }
    let mut value = input("irc.send");
    value["resource"]["buffer"] = json!("irc.docker.#private");
    assert_eq!(decide(&p, &value), DENY);
    value = input("http.request");
    value["request"]["method"] = json!("DELETE");
    assert_eq!(decide(&p, &value), DENY);
    value["request"]["method"] = json!("GET");
    value["request"]["host"] = json!("private.example");
    assert_eq!(decide(&p, &value), DENY);
    value = input("log");
    value["principal"]["id"] = json!("other");
    assert_eq!(decide(&p, &value), ALLOW);
}

#[test]
fn c_abi_rejects_nulls_and_overlength_before_reading_buffers() {
    // SAFETY: Nulls and overlength are explicitly rejected before dereference.
    unsafe {
        assert!(weechat_managed_policy_load(std::ptr::null(), 1, b"{}".as_ptr(), 2).is_null());
        assert!(weechat_managed_policy_load(
            b"x".as_ptr(),
            MAX_SOURCE_BYTES + 1,
            b"{}".as_ptr(),
            2
        )
        .is_null());
        assert_eq!(
            weechat_managed_policy_authorize(std::ptr::null(), b"{}".as_ptr(), 2),
            ERROR
        );
        weechat_managed_policy_free(std::ptr::null_mut());
    }
}

#[test]
fn c_abi_load_authorize_and_free_roundtrip() {
    let source = b"package weechat.managed\nallow := true";
    let data = b"{}";
    let input = serde_json::to_vec(&input("log")).unwrap();
    // SAFETY: Buffers/handle are valid for each call; free occurs exactly once.
    unsafe {
        let p =
            weechat_managed_policy_load(source.as_ptr(), source.len(), data.as_ptr(), data.len());
        assert!(!p.is_null());
        assert_eq!(
            weechat_managed_policy_authorize(p, input.as_ptr(), input.len()),
            ALLOW
        );
        assert_eq!(
            weechat_managed_policy_authorize(p, std::ptr::null(), 1),
            ERROR
        );
        weechat_managed_policy_free(p);
    }
}

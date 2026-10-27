# SPDX-License-Identifier: GPL-3.0-or-later
# This file and its data belong to the OPERATOR, never the managed script writer.
package weechat.managed

default allow := false

# Script names/generations are audit labels, not caller-selectable authority.
# Every script in this managed root receives the same operator-defined ceiling.
grant := data.managed

active if {
    input.principal.trust == "managed"
    input.principal.generation > 0
    input.phase == "active"
}

allow if {
    active
    input.operation == "event.receive"
    input.resource.buffer in grant.buffers
}

allow if {
    active
    input.operation == "irc.send"
    input.resource.buffer in grant.buffers
    count(input.request.text) <= 350
}

allow if {
    active
    input.operation == "http.request"
    input.request.method in grant.http_methods
    input.request.host in grant.http_hosts
}

allow if {
    active
    input.operation == "log"
    grant.log == true
}

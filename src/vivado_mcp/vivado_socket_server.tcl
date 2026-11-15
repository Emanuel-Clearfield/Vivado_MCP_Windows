################################################################################
# vivado_socket_server.tcl
#
# Startup script for the Vivado-hosted Tcl socket server.
#
# Launch Vivado with (the Python launcher does this for you):
#   vivado.bat -mode tcl -source vivado_socket_server.tcl -nojournal -log <file>
#
# Environment (all optional except where noted)
#   VIVADO_TCL_SERVER_HOST     bind address            (default 127.0.0.1)
#   VIVADO_TCL_SERVER_PORT     bind port               (default 9877)
#   VIVADO_TCL_SERVER_TOKEN    shared secret. When set, EVERY request must carry
#                              the same value in its "token" field, otherwise it
#                              is rejected and the connection is closed.
#   VIVADO_TCL_SERVER_VERBOSE  1 = print client connect/disconnect lines
#
# Ready signal
# ------------
# After the socket is bound ONE JSON line is written to stdout:
#   {"status": "ready", "host": "127.0.0.1", "port": 9877, "pid": 1234}
# or on failure:
#   {"status": "error", "message": "<reason>"}
#
# Wire protocol (line-delimited JSON, one JSON object per line)
# -------------------------------------------------------------
# Requests
#   {"op": "exec", "id": "<opaque>", "command": "<tcl>", "token": "<secret>"}
#   {"op": "ping", "token": "<secret>"}
#   {"op": "exit", "token": "<secret>"}
#   ("op" defaults to "exec" when a "command" field is present.)
#
# Responses (one line)
#   {"status": "ok",    "id": "..", "result": "<tcl result>", "error": null, "code": 0}
#   {"status": "error", "id": "..", "result": "",             "error": "<msg>", "code": 1}
#   {"status": "ok", "pong": true, "pid": 1234, "version": "2025.2"}   (ping)
#
# Console capture
# ---------------
# Vivado's own INFO/WARNING/ERROR messages are written below Tcl's channels, so
# they cannot be captured from inside Tcl (and `redirect` does not exist in
# 2025.2). Instead every exec prints two marker lines around the command:
#   @@VSS-BEGIN <id>@@   ...command output / Vivado messages...   @@VSS-END <id>@@
# The Python client launches Vivado with -log and slices the log file between
# the markers. The END marker doubles as a "log is flushed up to here" signal.
#
# Security
# --------
# Binds to loopback by default. Vivado Tcl has full filesystem / process access:
# never expose this port. The token defeats cross-protocol attacks (e.g. a web
# page POSTing a JSON body to 127.0.0.1:<port>); lines that are not a JSON
# object are dropped without a reply.
################################################################################

package require Tcl 8.5

proc vss_env {name default} {
    if {[info exists ::env($name)]} { return $::env($name) }
    return $default
}

set ::vss_host    [vss_env VIVADO_TCL_SERVER_HOST 127.0.0.1]
set ::vss_port    [vss_env VIVADO_TCL_SERVER_PORT 9877]
set ::vss_token   [vss_env VIVADO_TCL_SERVER_TOKEN ""]
set ::vss_verbose [vss_env VIVADO_TCL_SERVER_VERBOSE 0]

# Line buffering keeps our own output in order with Vivado's message stream.
catch {fconfigure stdout -buffering line}
catch {fconfigure stderr -buffering line}

# Track connected client channels so we can clean them up on exit.
set ::vss_clients {}

# -- JSON: encoding ------------------------------------------------------------

proc vss_json_escape {str} {
    set str [string map [list \
        "\\" "\\\\" "\"" "\\\"" "\n" "\\n" "\r" "\\r" "\t" "\\t" \
        "\b" "\\b" "\f" "\\f" "\u0000" "\\u0000"] $str]
    # Remaining control characters (ESC, etc.) must be \u00XX escaped or the
    # reply is invalid JSON.
    if {[regexp {[\u0001-\u001f]} $str]} {
        set out ""
        foreach ch [split $str ""] {
            scan $ch %c cp
            if {$cp < 32} {
                append out [format "\\u%04x" $cp]
            } else {
                append out $ch
            }
        }
        set str $out
    }
    return $str
}

proc vss_ok_json {id result code} {
    return "{\"status\": \"ok\", \"id\": \"[vss_json_escape $id]\", \"result\": \"[vss_json_escape $result]\", \"error\": null, \"code\": $code}"
}

proc vss_err_json {id msg code {result ""}} {
    return "{\"status\": \"error\", \"id\": \"[vss_json_escape $id]\", \"result\": \"[vss_json_escape $result]\", \"error\": \"[vss_json_escape $msg]\", \"code\": $code}"
}

# -- JSON: decoding ------------------------------------------------------------

# Decode the BODY of a JSON string (no surrounding quotes).
proc vss_json_unescape {s} {
    if {[string first "\\" $s] < 0} { return $s }
    set out ""
    set i 0
    set n [string length $s]
    while {$i < $n} {
        set j [string first "\\" $s $i]
        if {$j < 0} { append out [string range $s $i end]; break }
        if {$j > $i} { append out [string range $s $i [expr {$j - 1}]] }
        set c [string index $s [expr {$j + 1}]]
        set i [expr {$j + 2}]
        switch -exact -- $c {
            n { append out "\n" }
            r { append out "\r" }
            t { append out "\t" }
            b { append out "\b" }
            f { append out "\f" }
            u {
                set hex [string range $s $i [expr {$i + 3}]]
                if {![regexp {^[0-9a-fA-F]{4}$} $hex]} { error "bad \\u escape" }
                scan $hex %x cp
                incr i 4
                # UTF-16 surrogate pair -> one code point
                if {$cp >= 0xD800 && $cp <= 0xDBFF && [string range $s $i [expr {$i + 1}]] eq "\\u"} {
                    set hex2 [string range $s [expr {$i + 2}] [expr {$i + 5}]]
                    if {[regexp {^[0-9a-fA-F]{4}$} $hex2]} {
                        scan $hex2 %x lo
                        if {$lo >= 0xDC00 && $lo <= 0xDFFF} {
                            set cp [expr {0x10000 + (($cp - 0xD800) << 10) + ($lo - 0xDC00)}]
                            incr i 6
                        }
                    }
                }
                append out [format %c $cp]
            }
            default { append out $c }
        }
    }
    return $out
}

# Parse a FLAT JSON object (string / number / true / false / null values) into
# a dict. Top-level keys only, so text inside the "command" string can never be
# mistaken for another field (e.g. a forged "token").
proc vss_json_parse {line} {
    if {![regexp -start 0 -indices {\A\s*\{\s*} $line m]} { error "not a JSON object" }
    set pos [expr {[lindex $m 1] + 1}]
    set result [dict create]
    if {[regexp -start $pos -indices {\A\}\s*\Z} $line m]} { return $result }
    while 1 {
        if {![regexp -start $pos -indices {\A"((?:[^"\\]|\\.)*)"\s*:\s*} $line m kr]} {
            error "expected a key at offset $pos"
        }
        set key [vss_json_unescape [string range $line [lindex $kr 0] [lindex $kr 1]]]
        set pos [expr {[lindex $m 1] + 1}]
        if {[regexp -start $pos -indices {\A"((?:[^"\\]|\\.)*)"} $line m vr]} {
            set val [vss_json_unescape [string range $line [lindex $vr 0] [lindex $vr 1]]]
        } elseif {[regexp -start $pos -indices {\A(-?[0-9][0-9eE+.\-]*|true|false|null)} $line m vr]} {
            set val [string range $line [lindex $vr 0] [lindex $vr 1]]
        } else {
            error "unsupported value for \"$key\""
        }
        dict set result $key $val
        set pos [expr {[lindex $m 1] + 1}]
        if {[regexp -start $pos -indices {\A\s*,\s*} $line m]} {
            set pos [expr {[lindex $m 1] + 1}]
            continue
        }
        if {[regexp -start $pos -indices {\A\s*\}\s*\Z} $line m]} { break }
        error "expected , or \} at offset $pos"
    }
    return $result
}

# -- Per-client handler --------------------------------------------------------

proc vss_send {chan json} {
    if {[catch {puts $chan $json; flush $chan}]} { return 0 }
    return 1
}

proc vss_client_readable {chan addr port} {
    if {[catch {gets $chan line} nbytes] || [eof $chan]} {
        vss_close_client $chan $addr $port
        return
    }
    if {$nbytes < 0} {
        # Non-blocking channel: -1 with fblocked means "line not complete yet"
        # (large commands arrive in several chunks). Wait for more data.
        if {[fblocked $chan]} { return }
        vss_close_client $chan $addr $port
        return
    }

    set line [string trim $line]
    if {$line eq ""} { return }

    # Not a JSON object (e.g. the request line / headers of an HTTP request):
    # not for us. Drop the connection without replying.
    if {[string index $line 0] ne "\{"} {
        vss_close_client $chan $addr $port
        return
    }

    if {[catch {vss_json_parse $line} req]} {
        vss_send $chan [vss_err_json "" "Malformed JSON request: $req" 1]
        return
    }

    # Authentication
    if {$::vss_token ne ""} {
        set tok [expr {[dict exists $req token] ? [dict get $req token] : ""}]
        if {$tok ne $::vss_token} {
            vss_send $chan [vss_err_json "" "unauthorized" 1]
            vss_close_client $chan $addr $port
            return
        }
    }

    set id [expr {[dict exists $req id] ? [dict get $req id] : ""}]
    if {[dict exists $req op]} {
        set op [dict get $req op]
    } elseif {[dict exists $req command]} {
        set op exec
    } else {
        set op ""
    }

    switch -exact -- $op {
        ping {
            set ver ""
            catch {set ver [version -short]}
            vss_send $chan "{\"status\": \"ok\", \"pong\": true, \"pid\": [pid], \"version\": \"[vss_json_escape $ver]\"}"
        }
        exit {
            vss_send $chan [vss_ok_json $id "Vivado shutting down." 0]
            after 200
            vss_shutdown
        }
        exec {
            vss_exec $chan $addr $port $id [expr {[dict exists $req command] ? [dict get $req command] : ""}]
        }
        default {
            vss_send $chan [vss_err_json $id "Unknown or missing op/command in request." 1]
        }
    }
}

proc vss_exec {chan addr port id cmd} {
    if {$cmd eq ""} {
        vss_send $chan [vss_err_json $id "Empty command." 1]
        return
    }

    # Echo for people watching the Vivado window / log (outside the markers).
    puts stdout "Vivado% $cmd"
    puts stdout "@@VSS-BEGIN $id@@"
    flush stdout

    # Run in the global scope. rc: 0 ok, 1 error, 2 `return`, 3 break, 4 continue.
    set rc [catch {uplevel #0 $cmd} result]

    flush stdout
    flush stderr
    puts stdout "@@VSS-END $id@@"
    flush stdout

    if {$rc == 0 || $rc == 2} {
        if {$result ne ""} { puts stdout $result; flush stdout }
        set response [vss_ok_json $id $result $rc]
    } else {
        if {$rc == 3} {
            set result "invoked \"break\" outside of a loop"
        } elseif {$rc == 4} {
            set result "invoked \"continue\" outside of a loop"
        }
        puts stderr "ERROR: $result"
        flush stderr
        set response [vss_err_json $id $result $rc]
    }

    if {![vss_send $chan $response]} {
        vss_close_client $chan $addr $port
    }
}

proc vss_close_client {chan addr port} {
    catch {close $chan}
    set idx [lsearch -exact $::vss_clients $chan]
    if {$idx >= 0} {
        set ::vss_clients [lreplace $::vss_clients $idx $idx]
    }
    if {$::vss_verbose} {
        puts stdout "VivadoTCLServer: client $addr:$port disconnected."
        flush stdout
    }
}

# -- Accept callback -----------------------------------------------------------

proc vss_accept {chan addr port} {
    if {$::vss_verbose} {
        puts stdout "VivadoTCLServer: client connected from $addr:$port"
        flush stdout
    }
    lappend ::vss_clients $chan
    fconfigure $chan \
        -buffering line \
        -translation auto \
        -encoding utf-8 \
        -blocking 0
    fileevent $chan readable [list vss_client_readable $chan $addr $port]
}

# -- Graceful shutdown ---------------------------------------------------------

proc vss_shutdown {} {
    puts stdout "VivadoTCLServer: shutting down."
    flush stdout
    foreach chan $::vss_clients { catch {close $chan} }
    catch {close $::vss_server_sock}
    catch {exit}
}

# -- Start the server and emit the ready signal --------------------------------

if {[catch {
    set ::vss_server_sock [socket -server vss_accept -myaddr $::vss_host $::vss_port]
} err]} {
    puts stdout "{\"status\": \"error\", \"message\": \"[vss_json_escape $err]\"}"
    flush stdout
} else {
    puts stdout "{\"status\": \"ready\", \"host\": \"$::vss_host\", \"port\": $::vss_port, \"pid\": [pid]}"
    flush stdout
}

# Keep the Tcl event loop running so fileevent callbacks fire.
vwait ::_vss_never

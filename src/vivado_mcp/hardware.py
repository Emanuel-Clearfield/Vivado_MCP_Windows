"""Vivado Hardware Manager: list JTAG targets/devices and program an FPGA with a bitstream.

Each call is one Tcl script run as a normal background job, so the caller gets the usual
console capture and can collect a slow first connect with job_status.

Verified on Vivado 2025.2 (no board attached):
  * open_hw_manager is idempotent; the first call takes ~18 s.
  * connect_hw_server launches hw_server (~10 s) and fails with [Labtoolstcl 44-586] when that
    server is already connected, so the script checks get_hw_servers first and otherwise calls
    refresh_hw_server (which also picks up a board plugged in after connecting).
  * get_hw_targets with no cable throws [Labtoolstcl 44-199]; get_hw_devices returns empty.
On a Zynq board (ZedBoard) the JTAG chain also holds the ARM debug port (arm_dap_0), so the FPGA
is chosen by excluding ARM DAP entries rather than trusting current_hw_device.

Scripts return "key=value" lines; parse_list_result / parse_program_result turn them into fields.
Our own failures are raised as Tcl errors starting with "VMCP: " (see explain_error).
"""

from __future__ import annotations

import re
from pathlib import Path

DEFAULT_HW_SERVER = "localhost:3121"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/*\-]+$")


def _check_name(value: str, what: str) -> str:
    value = (value or "").strip()
    if value and not _NAME_RE.match(value):
        raise ValueError(f"{what} contains unsupported characters: {value!r}")
    return value


def _tcl_path(path: str, what: str, suffix: str) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute():
        raise ValueError(f"{what} must be an absolute path: {path}")
    if not p.is_file():
        raise ValueError(f"{what} not found: {p}")
    if p.suffix.lower() != suffix:
        raise ValueError(f"{what} must be a {suffix} file: {p}")
    text = p.as_posix()
    if any(c in text for c in '{}"[]$\\'):
        raise ValueError(f"{what} path contains characters Tcl cannot take safely: {text}")
    return text


_PRELUDE = r"""
namespace eval ::vivado_mcp {}
proc ::vivado_mcp::hw_connect {url} {
    open_hw_manager
    set want [string map {TCP: {} tcp: {}} $url]
    if {[lsearch -exact [get_hw_servers -quiet] $want] < 0} {
        foreach s [get_hw_servers -quiet] { catch {disconnect_hw_server $s} }
        connect_hw_server -url $want -allow_non_jtag
    } else {
        refresh_hw_server [get_hw_servers $want]
    }
    if {[catch {get_hw_targets} targets] || [llength $targets] == 0} {
        error "VMCP: No JTAG target found on hw_server $want. Check that the board is powered\
 on and connected through its JTAG/PROG USB port, and that the Digilent cable drivers\
 are installed."
    }
    return $targets
}
proc ::vivado_mcp::open_target {t} {
    catch {close_hw_target}
    open_hw_target $t
}
proc ::vivado_mcp::fpgas {} {
    set out {}
    foreach d [get_hw_devices -quiet] {
        if {[string match -nocase arm* [get_property PART $d]] || [string match -nocase arm* $d]} {
            continue
        }
        lappend out $d
    }
    return $out
}
proc ::vivado_mcp::done {d} {
    set v ?
    catch {set v [get_property REGISTER.IR.BIT5_DONE $d]}
    return $v
}
proc ::vivado_mcp::newest {files} {
    set best {}
    set best_t -1
    foreach f $files {
        if {[file mtime $f] > $best_t} { set best_t [file mtime $f]; set best $f }
    }
    return $best
}
"""


def _wrap(body: str) -> str:
    """Run the body inside `apply` so its variables (t, d, ...) never touch the user's globals."""
    return _PRELUDE + "apply {{} {\n" + body + "}}\n"


def build_list_script(hw_server_url: str = DEFAULT_HW_SERVER) -> str:
    url = _check_name(hw_server_url, "hw_server_url") or DEFAULT_HW_SERVER
    return _wrap(
        "set ::vivado_mcp::r [list server=" + url + "]\n"
        + "foreach t [::vivado_mcp::hw_connect {" + url + "}] {\n"
        + r"""    lappend ::vivado_mcp::r "target=$t"
    if {[catch {::vivado_mcp::open_target $t} msg]} {
        lappend ::vivado_mcp::r "target_error=$t: [string map {\n { }} [string trim $msg]]"
        continue
    }
    set fp [::vivado_mcp::fpgas]
    foreach d [get_hw_devices -quiet] {
        set is_fpga [expr {[lsearch -exact $fp $d] >= 0}]
        set part [get_property PART $d]
        lappend ::vivado_mcp::r "device=$t|$d|$part|$is_fpga|[::vivado_mcp::done $d]"
    }
}
return [join $::vivado_mcp::r \n]
"""
    )


def build_program_script(
    bitstream: str = "",
    device: str = "",
    probes: str = "",
    run: str = "impl_1",
    target: str = "",
    hw_server_url: str = DEFAULT_HW_SERVER,
) -> str:
    url = _check_name(hw_server_url, "hw_server_url") or DEFAULT_HW_SERVER
    device = _check_name(device, "device")
    target = _check_name(target, "target")
    run = _check_name(run, "run") or "impl_1"
    bit = _tcl_path(bitstream, "bitstream", ".bit") if bitstream else ""
    ltx = _tcl_path(probes, "probes", ".ltx") if probes else ""

    parts = ["set ::vivado_mcp::r {}\n"]
    if bit:
        parts.append("set ::vivado_mcp::bit {" + bit + "}\n")
    else:
        parts.append(
            "set ::vivado_mcp::run {" + run + "}\n"
            + r"""if {[catch {current_project}]} {
    error "VMCP: No bitstream given and no project is open. Pass bitstream=<absolute path to .bit>\
 or open the project first."
}
set ro [get_runs -quiet $::vivado_mcp::run]
if {$ro eq ""} { error "VMCP: Run '$::vivado_mcp::run' does not exist in the open project." }
set bits [glob -nocomplain -directory [get_property DIRECTORY $ro] *.bit]
set ::vivado_mcp::bit [::vivado_mcp::newest $bits]
if {$::vivado_mcp::bit eq ""} {
    error "VMCP: No .bit file in run '$::vivado_mcp::run' (status: [get_property STATUS $ro]).\
 Generate it first: launch_runs $::vivado_mcp::run -to_step write_bitstream"
}
lappend ::vivado_mcp::r "run=$::vivado_mcp::run" "run_status=[get_property STATUS $ro]"
lappend ::vivado_mcp::r "run_needs_refresh=[get_property NEEDS_REFRESH $ro]"
"""
        )
    parts.append(
        r"""if {![file isfile $::vivado_mcp::bit]} {
    error "VMCP: Bitstream not found: $::vivado_mcp::bit"
}
"""
        + "set targets [::vivado_mcp::hw_connect {" + url + "}]\n"
    )
    if target:
        parts.append(
            "set t [get_hw_targets -quiet {*" + target + "*}]\n"
            + r"""if {[llength $t] != 1} {
    error "VMCP: target filter matches [llength $t] of the JTAG targets: $targets"
}
"""
        )
    else:
        parts.append(
            r"""if {[llength $targets] != 1} {
    error "VMCP: Several JTAG targets: $targets. Pass target=<name> (see list_hw_devices)."
}
set t [lindex $targets 0]
"""
        )
    parts.append(
        r"""::vivado_mcp::open_target $t
set all [::vivado_mcp::fpgas]
"""
    )
    if device:
        parts.append(
            "set d [lsearch -all -inline -glob $all {" + device + "}]\n"
            + r"""if {[llength $d] != 1} {
    error "VMCP: device filter matches [llength $d] of the FPGAs on the chain: $all"
}
"""
        )
    else:
        parts.append(
            r"""if {[llength $all] != 1} {
    error "VMCP: Expected one FPGA on the JTAG chain, found [llength $all]: $all\
 (all devices: [get_hw_devices -quiet]). Pass device=<name>."
}
set d [lindex $all 0]
"""
        )
    parts.append(
        r"""set d [get_hw_devices $d]
current_hw_device $d
refresh_hw_device -update_hw_probes false $d
set_property PROGRAM.FILE $::vivado_mcp::bit $d
"""
        + "set_property PROBES.FILE {" + ltx + "} $d\n"
        + "set_property FULL_PROBES.FILE {" + ltx + "} $d\n"
        + r"""program_hw_devices $d
refresh_hw_device $d
lappend ::vivado_mcp::r "target=$t" "device=$d" "part=[get_property PART $d]"
lappend ::vivado_mcp::r "bitstream=$::vivado_mcp::bit" "done=[::vivado_mcp::done $d]"
"""
        + "lappend ::vivado_mcp::r {probes=" + ltx + "} {server=" + url + "}\n"
        + "return [join $::vivado_mcp::r \\n]\n"
    )
    return _wrap("".join(parts))


def _kv_lines(text: str):
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            yield key.strip(), value


def _bool(value: str) -> bool | None:
    return {"1": True, "0": False}.get(value.strip())


def parse_list_result(text: str) -> dict[str, object]:
    out: dict[str, object] = {"targets": [], "devices": []}
    for key, value in _kv_lines(text):
        if key == "server":
            out["server"] = value
        elif key == "target":
            out["targets"].append(value)
        elif key == "target_error":
            out.setdefault("target_errors", []).append(value)
        elif key == "device" and value.count("|") == 4:
            target, name, part, is_fpga, done = value.split("|")
            out["devices"].append(
                {
                    "target": target,
                    "name": name,
                    "part": part,
                    "is_fpga": is_fpga == "1",
                    "done": _bool(done),
                }
            )
    return out


def parse_program_result(text: str) -> dict[str, object]:
    out: dict[str, object] = dict(_kv_lines(text))
    if "done" in out:
        out["done"] = _bool(str(out["done"]))
    if "run_needs_refresh" in out:
        out["run_needs_refresh"] = _bool(str(out["run_needs_refresh"]))
    if out.get("probes") == "":
        out["probes"] = None
    return out


def explain_error(error: str | None) -> str | None:
    """Reduce our own 'VMCP:' Tcl errors to their message; leave Vivado errors as they are."""
    if not error:
        return error
    m = re.search(r"VMCP: (.*)", error, re.S)
    return m.group(1).strip() if m else error

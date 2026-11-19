"""Hardware Manager scripts (list_hw_devices / program_device).

The generated Tcl is executed for real under tclsh against stub Hardware Manager commands that
model a ZedBoard JTAG chain (arm_dap_0 + xc7z020_1), an absent board, and a two-FPGA chain.
Stub behaviour follows what Vivado 2025.2 did live: get_hw_targets throws when there is no cable,
connect_hw_server throws when that server is already connected.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vivado_mcp import hardware, server


def _find_tclsh():
    found = shutil.which("tclsh") or shutil.which("tclsh8.6")
    if found:
        return found
    git = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git/mingw64/bin/tclsh.exe"
    return str(git) if git.is_file() else None


TCLSH = _find_tclsh()
needs_tcl = pytest.mark.skipif(TCLSH is None, reason="tclsh not available")

STUBS = r"""
# --- stub Vivado Hardware Manager --------------------------------------------------------
# Configured by: ::chain (list of {name part}), ::servers, ::project_dir, ::bits
set ::log {}
set ::opened {}
set ::props [dict create]
proc log {args} { lappend ::log [join $args " "] }
proc open_hw_manager {} { log open_hw_manager }
proc get_hw_servers {args} {
    set args [lsearch -all -inline -not -exact $args -quiet]
    if {[llength $args]} { return [lsearch -all -inline -exact $::servers [lindex $args end]] }
    return $::servers
}
proc connect_hw_server {args} {
    set url [lindex $args [expr {[lsearch $args -url] + 1}]]
    if {[lsearch -exact $::servers $url] >= 0} { error "ERROR: \[Labtoolstcl 44-586\] Disconnect" }
    lappend ::servers $url
    log connect $url
}
proc disconnect_hw_server {s} { log disconnect $s }
proc refresh_hw_server {s} { log refresh_server $s }
proc get_hw_targets {args} {
    if {![llength $::targets]} { error "ERROR: \[Labtoolstcl 44-199\] No matching targets found" }
    set pat [expr {[llength $args] ? [lindex $args end] : "*"}]
    return [lsearch -all -inline -glob $::targets $pat]
}
proc close_hw_target {} { if {$::opened eq ""} { error "no open target" }; set ::opened {} }
proc open_hw_target {t} { set ::opened $t; log open_target $t }
proc get_hw_devices {args} {
    set names {}
    foreach dev $::chain { lappend names [lindex $dev 0] }
    set args [lsearch -all -inline -not -exact $args -quiet]
    if {[llength $args]} { return [lsearch -all -inline -exact $names [lindex $args 0]] }
    return $names
}
proc get_property {name obj} {
    foreach dev $::chain {
        if {[lindex $dev 0] eq $obj} {
            if {$name eq "PART"} { return [lindex $dev 1] }
            if {$name eq "REGISTER.IR.BIT5_DONE"} {
                return [expr {[dict exists $::props $obj programmed] ? 1 : 0}]
            }
        }
    }
    switch -- $name {
        DIRECTORY { return $::project_dir }
        STATUS { return "write_bitstream Complete!" }
        NEEDS_REFRESH { return 0 }
    }
    error "stub get_property: $name $obj"
}
proc set_property {name value obj} { dict set ::props $obj $name $value; log set $name $value $obj }
proc current_hw_device {d} { log current $d }
proc refresh_hw_device {args} { log refresh_device [lindex $args end] }
proc program_hw_devices {d} {
    if {![dict exists $::props $d PROGRAM.FILE]} { error "no PROGRAM.FILE" }
    dict set ::props $d programmed 1
    log program $d [dict get $::props $d PROGRAM.FILE]
}
proc current_project {} { if {$::project_dir eq ""} { error "no project" }; return proj }
proc get_runs {args} { if {[lindex $args end] eq "impl_1"} { return impl_1 }; return {} }
"""

ZEDBOARD = "{arm_dap_0 arm_dap} {xc7z020_1 xc7z020}"


def run_tcl(tmp_path, script, chain=ZEDBOARD, targets="localhost:3121/xilinx_tcf/Digilent/210248",
            servers="", project_dir=""):
    driver = tmp_path / "driver.tcl"
    driver.write_text(
        STUBS
        + f"set ::chain {{{chain}}}\nset ::targets {{{targets}}}\nset ::servers {{{servers}}}\n"
        + f"set ::project_dir {{{project_dir}}}\nset ::user_t keep\n"
        + "set rc [catch {source [lindex $argv 0]} res]\n"
        + 'puts "RC=$rc"\nputs "LOG=[join $::log {;}]"\n'
        + 'puts "T=[info exists ::t] D=[info exists ::d]"\n'
        + 'puts "RESULT_BEGIN"\nputs $res\n',
        encoding="utf-8",
    )
    (tmp_path / "script.tcl").write_text(script, encoding="utf-8")
    out = subprocess.run(
        [TCLSH, str(driver), str(tmp_path / "script.tcl")],
        capture_output=True, text=True, timeout=30,
    ).stdout
    head, _, result = out.partition("RESULT_BEGIN\n")
    lines = [line for line in head.splitlines() if "=" in line and not line.startswith("T=")]
    info = dict(line.split("=", 1) for line in lines)
    return int(info["RC"]), info["LOG"], result.strip(), head


@pytest.fixture
def bitfile(tmp_path):
    bit = tmp_path / "top.bit"
    bit.write_bytes(b"\x00")
    return bit


# --- argument validation (no Tcl needed) -----------------------------------------------------


def test_bitstream_must_exist_and_be_absolute(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        hardware.build_program_script(bitstream="top.bit")
    with pytest.raises(ValueError, match="not found"):
        hardware.build_program_script(bitstream=str(tmp_path / "missing.bit"))
    wrong = tmp_path / "top.bin"
    wrong.write_bytes(b"")
    with pytest.raises(ValueError, match=r"\.bit"):
        hardware.build_program_script(bitstream=str(wrong))


def test_names_are_validated():
    with pytest.raises(ValueError, match="device"):
        hardware.build_program_script(device="x]; exec calc")
    with pytest.raises(ValueError, match="hw_server_url"):
        hardware.build_list_script("host:3121 {")


def test_parsers_and_error_cleanup():
    listed = hardware.parse_list_result(
        "server=localhost:3121\ntarget=T1\n"
        "device=T1|arm_dap_0|arm_dap|0|?\ndevice=T1|xc7z020_1|xc7z020|1|1"
    )
    assert listed["targets"] == ["T1"]
    assert listed["devices"][0] == {
        "target": "T1", "name": "arm_dap_0", "part": "arm_dap", "is_fpga": False, "done": None
    }
    assert listed["devices"][1]["is_fpga"] and listed["devices"][1]["done"] is True
    prog = hardware.parse_program_result("device=xc7z020_1\ndone=1\nprobes=\nrun_needs_refresh=0")
    assert prog == {"device": "xc7z020_1", "done": True, "probes": None, "run_needs_refresh": False}
    msg = hardware.explain_error("ERROR: x\nVMCP: No JTAG target found.")
    assert msg == "No JTAG target found."
    assert hardware.explain_error("ERROR: [Labtools 27-1] real") == "ERROR: [Labtools 27-1] real"


# --- generated Tcl, executed against the stubs ------------------------------------------------


@needs_tcl
def test_program_zedboard_skips_arm_dap(tmp_path, bitfile):
    rc, log, result, head = run_tcl(tmp_path, hardware.build_program_script(bitstream=str(bitfile)))
    assert rc == 0, result
    assert "connect localhost:3121" in log
    assert f"program xc7z020_1 {bitfile.as_posix()}" in log
    assert "arm_dap_0" not in log
    parsed = hardware.parse_program_result(result)
    assert parsed["device"] == "xc7z020_1" and parsed["part"] == "xc7z020"
    assert parsed["done"] is True and parsed["probes"] is None
    assert "T=0 D=0" in head  # nothing leaked into the user's global variables


@needs_tcl
def test_program_reuses_connected_server(tmp_path, bitfile):
    rc, log, result, _ = run_tcl(
        tmp_path, hardware.build_program_script(bitstream=str(bitfile)), servers="localhost:3121"
    )
    assert rc == 0, result
    entries = log.split(";")
    assert not any(e.startswith("connect ") for e in entries)
    assert "refresh_server localhost:3121" in entries


@needs_tcl
def test_no_board_gives_clear_error(tmp_path, bitfile):
    rc, log, result, _ = run_tcl(
        tmp_path, hardware.build_program_script(bitstream=str(bitfile)), targets=""
    )
    assert rc == 1 and "No JTAG target found" in result and "program" not in log
    rc, _, result, _ = run_tcl(tmp_path, hardware.build_list_script(), targets="")
    assert rc == 1 and "Digilent" in result


@needs_tcl
def test_two_fpgas_need_device_argument(tmp_path, bitfile):
    chain = "{xc7a35t_0 xc7a35t} {xc7a100t_1 xc7a100t}"
    rc, _, result, _ = run_tcl(
        tmp_path, hardware.build_program_script(bitstream=str(bitfile)), chain=chain
    )
    assert rc == 1 and "Expected one FPGA" in result and "xc7a100t_1" in result
    rc, log, result, _ = run_tcl(
        tmp_path, hardware.build_program_script(bitstream=str(bitfile), device="xc7a100t*"),
        chain=chain,
    )
    assert rc == 0, result
    assert "program xc7a100t_1" in log


@needs_tcl
def test_program_from_project_run_picks_newest_bit(tmp_path):
    runs = tmp_path / "impl_1"
    runs.mkdir()
    old, new = runs / "old.bit", runs / "top.bit"
    old.write_bytes(b"")
    new.write_bytes(b"")
    os.utime(old, (1_000_000, 1_000_000))
    rc, log, result, _ = run_tcl(
        tmp_path, hardware.build_program_script(), project_dir=runs.as_posix()
    )
    assert rc == 0, result
    parsed = hardware.parse_program_result(result)
    assert parsed["bitstream"] == new.as_posix()
    assert parsed["run"] == "impl_1" and parsed["run_needs_refresh"] is False


@needs_tcl
def test_program_without_project_or_bitstream(tmp_path):
    rc, _, result, _ = run_tcl(tmp_path, hardware.build_program_script())
    assert rc == 1 and "no project is open" in result


@needs_tcl
def test_list_zedboard(tmp_path):
    rc, log, result, _ = run_tcl(tmp_path, hardware.build_list_script())
    assert rc == 0, result
    listed = hardware.parse_list_result(result)
    assert [d["name"] for d in listed["devices"]] == ["arm_dap_0", "xc7z020_1"]
    assert [d["is_fpga"] for d in listed["devices"]] == [False, True]
    assert "program" not in log and "set " not in log  # listing never changes the board


# --- MCP tool wrapper ---------------------------------------------------------------------------


def test_program_device_tool_reports_bad_path_without_vivado():
    out = asyncio.run(server.program_device(bitstream="relative.bit"))
    assert out["status"] == "error" and "absolute" in out["error"]

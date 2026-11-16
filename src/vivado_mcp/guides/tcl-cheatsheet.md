# Tcl / Vivado cheat sheet

## Objects and properties
```tcl
get_projects; current_project; get_files; get_filesets; get_runs
get_cells -hierarchical *; get_nets *; get_ports *; get_pins -of [get_cells u1]
get_property <PROP> <object>      ;# e.g. get_property STATUS [get_runs synth_1]
set_property <PROP> <value> <object>
list_property <object>            ;# all properties of an object
report_property <object>
```

## Messages and diagnostics
```tcl
send_msg_id {Test 1-1} INFO {text}     ;# emits a Vivado message
get_msg_config -rules                  ;# message limits / severities
set_msg_config -id {Synth 8-3332} -severity INFO -suppress
```
Vivado 2025.2 has no `redirect` or `tee`. Use `report_* -file X` to write to a file.

## Help
```tcl
help                       ;# categories
help launch_runs           ;# one command
launch_runs -help          ;# options
info commands *synth*      ;# find commands by pattern
version
```

## Plain Tcl you will use
```tcl
set x 5; puts "x=$x"
foreach f [get_files] { puts $f }
if {[catch {some_command} err]} { puts "failed: $err" }
file exists C:/work/a.txt; file mkdir C:/work/out; glob C:/work/src/*.v
pwd; cd C:/work
```
Prefer braces for command bodies and forward slashes in paths.

## Part and project info
```tcl
get_parts xc7a35t*                     ;# find parts
get_property PART [current_project]
report_ip_status; get_ips
```

## Cleaning up
```tcl
close_project
reset_run synth_1
close_sim -force
```

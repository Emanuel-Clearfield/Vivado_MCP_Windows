# Typical FPGA workflow (Vivado 2025.2, project mode)

Run each step with `run_tcl_command`. Steps marked LONG take minutes: expect
`status: running`, then poll `job_status(wait=60)`.

## 1. Project
```tcl
create_project my_proj C:/work/my_proj -part xc7a35tcpg236-1 -force
add_files {C:/work/src/top.v C:/work/src/sub.v}
add_files -fileset constrs_1 C:/work/constraints/top.xdc
add_files -fileset sim_1 C:/work/sim/tb_top.v
set_property top top [current_fileset]
update_compile_order -fileset sources_1
```
Use forward slashes in paths. To reopen: `open_project C:/work/my_proj/my_proj.xpr`.
Inspect: `current_project`, `get_files`, `get_property PART [current_project]`.

## 2. Simulate (optional, use `run_simulation` or by hand)
```tcl
set_property top tb_top [get_filesets sim_1]
launch_simulation
run all
close_sim
```

## 3. Synthesis (LONG)
```tcl
reset_run synth_1
launch_runs synth_1 -jobs 4
wait_on_run synth_1
```
`wait_on_run` blocks Vivado until done, which is fine: the job stays open and you collect it
with `job_status`. Check afterwards:
```tcl
get_property STATUS   [get_runs synth_1]
get_property PROGRESS [get_runs synth_1]
```
Status `synth_design Complete!` = success. Read warnings with `console_tail` or open
`<proj>.runs/synth_1/runme.log` through `read_project_artifact`.

## 4. Implementation (LONG)
```tcl
launch_runs impl_1 -jobs 4
wait_on_run impl_1
get_property STATUS [get_runs impl_1]
```

## 5. Reports
```tcl
open_run impl_1
report_timing_summary -max_paths 10 -file C:/work/out/timing.rpt
report_utilization -file C:/work/out/util.rpt
report_drc -file C:/work/out/drc.rpt
```
Timing met when WNS >= 0 and WHS >= 0. Query directly:
`get_property SLACK [get_timing_paths -max_paths 1 -nworst 1]`.

## 6. Bitstream (LONG)
```tcl
launch_runs impl_1 -to_step write_bitstream -jobs 4
wait_on_run impl_1
```
Output: `<proj>.runs/impl_1/<top>.bit`.

## 7. Program the board
Use the tools, not hand-written Tcl:
- `list_hw_devices()`: connects to hw_server (first call ~30 s) and lists the JTAG chain. On a
  ZedBoard: `arm_dap_0` (Zynq ARM debug port, `is_fpga: false`) and `xc7z020_1` (the FPGA).
- `program_device()`: programs the newest `.bit` of `impl_1` in the open project, or
  `program_device(bitstream="C:/work/my_proj/my_proj.runs/impl_1/top.bit")`. It skips `arm_dap`
  automatically, sets an optional `.ltx` probes file, and returns `done: true` when the DONE pin is high.
  `run_needs_refresh: true` means the bitstream is older than the sources: rebuild first.

Programming is volatile (lost at power-off; flash and SD card are untouched). The part in the
project (`xc7z020clg484-1` for ZedBoard) must match the board, or `program_hw_devices` fails.
Hand-written Tcl pitfall: on Zynq boards `[current_hw_device]` can be `arm_dap_0`, not the FPGA.

## Habits that save time
- After every step check `status`, then read `console` for `ERROR:` and `CRITICAL WARNING:`.
- Do not send several unrelated steps in one command: an ERROR aborts the rest of it.
- Prefer `-file` for big reports and read them with `read_project_artifact` or `console_tail`.
- Use `puts` to return values that a command only prints.

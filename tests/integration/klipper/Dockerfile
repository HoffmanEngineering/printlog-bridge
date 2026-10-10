# Klipper, a simulated printer board (simulavr) and Moonraker, from Mainsail's
# virtual-klipper-printer, pinned so a rebuild upstream can't change the tests under us.
FROM ghcr.io/mainsail-crew/virtual-klipper-printer@sha256:6c4dabb3eca5c20e8d98c6bd3b14db406b83dd3cf62539e409073e7a021198d1

# Changes so test prints run on a shared CI runner. The image's folders belong to root.
# - The simulated heaters never warm up, so let the extruder move cold: a test print then feeds
#   real filament, and Klipper's filament_used is what it would be on a printer.
# - The simulated board steps in real time, and on a busy runner homing, fast moves or
#   pressure advance's step bursts end in "Timer too close". So pressure advance is off, and
#   test prints don't home: they set their position with SET_KINEMATIC_POSITION, which needs
#   [force_move]. (printer.cfg ends in a SAVE_CONFIG block, so that goes in an included file.)
USER root
ARG EXTRUDER=/home/printer/example-configs/addons/single_extruder.cfg
RUN sed -i -e 's/^min_extrude_temp: .*/min_extrude_temp: 0/' \
           -e 's/^pressure_advance: .*/pressure_advance: 0/' "$EXTRUDER" \
 && grep -q '^min_extrude_temp: 0$' "$EXTRUDER" \
 && grep -q '^pressure_advance: 0$' "$EXTRUDER" \
 && printf '\n[force_move]\nenable_force_move: True\n' >> "$EXTRUDER"
USER printer

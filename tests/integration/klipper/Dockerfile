# Klipper, a simulated printer board (simulavr) and Moonraker, from Mainsail's
# virtual-klipper-printer, pinned so a rebuild upstream can't change the tests under us.
FROM ghcr.io/mainsail-crew/virtual-klipper-printer@sha256:6c4dabb3eca5c20e8d98c6bd3b14db406b83dd3cf62539e409073e7a021198d1

# The simulated heaters never warm up, so let the extruder move cold: a test print then feeds
# real filament, and Klipper's filament_used is what it would be on a printer.
RUN sed -i 's/^min_extrude_temp: .*/min_extrude_temp: 0/' \
      /home/printer/example-configs/addons/single_extruder.cfg \
 && grep -q '^min_extrude_temp: 0$' /home/printer/example-configs/addons/single_extruder.cfg

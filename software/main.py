import machine, time

from badge_config import hardware_for, load_badge_config

time.sleep(0.1)
hardware = hardware_for(load_badge_config()["badge_version"])
if machine.Pin(hardware["select_pin"], machine.Pin.IN).value() == 0:
    print("Not starting main application")
else:
    import bsides25

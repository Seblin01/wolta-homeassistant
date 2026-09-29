## New – a repair when your energy data doesn't add up

Wolta derives your household consumption from your sensors: solar + grid import + battery
discharge − export − battery charge. When the battery takes in more energy than solar and the grid
deliver in the same interval, the result is negative — the sensors don't describe the whole house.
Measured across all Wolta installations, 15 of 54 had this in more than 11.8 % of their charged
energy, the next one at 8.4 %. For those, the grade measured the sensor mapping, not the battery.

Since the backend update of 29 September such grades are kept out of the comparison with other
installations. You still see your own grade. This release tells you **why**, in Home Assistant:

- **A repair per cause**, naming the sensors you picked:
  - *solar reads too low* — typically solar measured after a DC-coupled battery (use the inverter's
    DC input, not its AC output), or an inverter/string that isn't included;
  - *grid import or charge doesn't add up* — check that import covers all phases and that the
    charge sensor is the battery's own counter;
  - *a sensor seems to be missing* — the battery charges while neither solar nor import shows energy;
  - otherwise a general check of the mapping.
- **The backend decides.** The integration reads `energy_balance.excluded` and never applies its own
  threshold, so Home Assistant and wolta.se cannot disagree.
- **Not in view-only mode**, where the data comes from a webhook or bridge rather than your sensors.
- **It clears itself** once the grading window no longer contains the mismatched data.

Nothing is uploaded differently, and no setting changes.

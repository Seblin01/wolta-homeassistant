# v0.36.0 – simplified two-step setup, battery measured automatically

### What changes

**Setup is now two steps instead of three, and you're no longer asked for battery
capacity, power or efficiency.** Step 1 maps your energy sensors as before. Step 2
combines price zone, control system, data sharing and the charge/discharge
direction toggle into one form and creates the profile — there is no separate
privacy step, and no battery numbers to type. The battery is *declared* rather than
configured: Wolta measures usable capacity, peak power and round-trip efficiency
directly from the data your installation uploads (requires wolta.se api 0.86.0+).

**Two new statuses cover the measuring window.** `sensor.wolta_status` shows
*Measuring battery* while Wolta is gathering enough charge cycles to derive the
battery's parameters — typically 30 days with a few full charges. If the data
can't yield a measurement (the battery never charges fully, or one of the battery
streams is empty), the status switches to *Needs battery values* and a new fixable
Repair, "Wolta needs your battery's nameplate values", asks for the manufacturer's
nameplate capacity and power. Wolta derives the usable capacity from that pair
immediately and switches over to the measured value later if the data allows it.
The same fields can also be filled in proactively at any time under Configure →
Settings, without waiting for the repair to fire.

**The price zone is suggested, never preselected.** The zone guessed from your
Home Assistant location is moved to the top of the list and labelled, but the
field itself starts empty — you still have to choose it actively, since it can't
be changed once the plant exists.

**The control system is prefilled when Wolta recognises your battery
integration**, read from the platform behind the battery sensors you picked in
step 1. It's just a comparison label — it doesn't affect the grade — so change it
if it's wrong or leave it as suggested.

**The charge/discharge direction toggle is always shown in step 2** (previously it
only appeared as a separate confirmation step when your history looked reversed),
preselected when it does.

**A suggested purchase date, read from your sensor history, is now offered once in
the Configure dialog instead of being set automatically.** It shows up as a
suggested value the first time you open Settings after setup; accept it, type your
own, or leave the field blank — nothing is written until you save.

### Notes

- Linking an existing wolta.se profile is unaffected: that path still skips the
  battery/zone step entirely and reads everything from the server.
- No grade change for existing plants — this only affects new setups and the
  battery/purchase-date fields in Configure.
- No breaking changes. Update via HACS and restart Home Assistant.

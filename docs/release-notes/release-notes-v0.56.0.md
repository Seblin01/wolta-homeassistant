# v0.56.0 – Battery direction gets its own step

- **The "Battery charge/discharge reversed" box is gone from Settings.** It swapped charge and discharge in your whole stored history, so clearing it while saving other settings could quietly undo a correction made months earlier. Saving Settings now never changes the direction.
- **Configure → Battery direction.** A step of its own for mixed-up charge and discharge sensors. It shows which periods are reversed and what Wolta's state-of-charge check says, and offers to reverse the whole history, swap from a date, or store everything as the sensors report it. Before anything changes, it names the stored period and the number of days. Nothing is re-sent, the replaced values are kept for 90 days, and you can undo the change by choosing the opposite.
- **Wolta can refuse a swap that goes against the state of charge.** When the battery's state of charge shows the stored direction is right, the step asks you to tick *Change anyway* first. Without it, Wolta refuses the swap and a repair says why. The direction repair still swaps exactly the period Wolta has found reversed.
- **Reconfigure asks before a role swap.** Moving a sensor between battery charge and discharge rebuilds the stored battery data. Reconfigure now shows the period and recommends Battery direction instead, and only saves once you confirm.

Needs Wolta API 0.124.0 or newer. You do not need to do anything: your stored data and the direction your uploads use stay as they are.

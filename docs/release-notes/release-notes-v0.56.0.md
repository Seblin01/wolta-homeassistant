# v0.56.0 – Battery direction gets its own step

- **The "Battery charge/discharge reversed" box is gone from Settings**, since saving Settings could quietly swap charge and discharge in your whole stored history.
- **Configure → Battery direction** swaps charge and discharge for a period you choose and names the stored period before anything changes. When the battery's state of charge says the stored direction is right, you tick *Change anyway* first, or Wolta refuses the swap.
- **Nothing to do:** your stored data and the direction your uploads use stay as they are. Needs Wolta API 0.124.0 or newer.

# External control (Grid Rewards and similar flex-market services)

Some services take temporary direct control of your battery in exchange for compensation — Tibber's Grid Rewards is one example, and other flex-market programs work the same way. While such a service is dispatching your battery, the household isn't making its own price decisions, so those intervals shouldn't be judged as good or bad price calls in the optimisation grade. They are **not** cut out of the calculation: they are bound to your measured operation, so they contribute equally to both sides of the grade's ratio and neither raise nor lower your score. Removing them outright would break the battery's state-of-charge chain between intervals, since what the battery holds carries over from one interval to the next.

**What the picker does.** Both the setup flow's entity step and the **Reconfigure** option afterwards (Settings → Devices & Services → Wolta → three-dot menu → Reconfigure) offer an optional **External control active** field: point it at a `binary_sensor` that is `on` for exactly as long as a flex service has control of the battery. The integration reads that sensor's Home Assistant state history and marks the corresponding 15-minute intervals so the backend can neutralise them. Leaving the field empty — the default — changes nothing: no extra sensor is read, and every interval is graded exactly as it was before this feature existed.

**Where to get the sensor (Tibber Grid Rewards).** Tibber's official APIs do not expose
Grid Rewards at all — neither the GraphQL API nor the newer Data API has a field for it, so
there is nothing for Wolta to read directly. The community integration
[`JohNan/homeassistant-tibber_grid_rewards`](https://github.com/JohNan/homeassistant-tibber_grid_rewards)
fills the gap: it creates a **Grid Reward Active** binary sensor that is `on` for exactly as
long as Tibber reports the `GridRewardDelivering` state. Point the picker straight at it — no
template sensor needed.

Two things worth knowing before you rely on it. It talks to Tibber's *app* API (the one behind
the phone app, using your email and password) rather than an official, documented one, so it
can stop working without warning if Tibber changes something. And it reports the present
moment, not the past: Wolta reads the sensor's recorder history, so the flag only covers the
period since you installed that integration — dispatch sessions from before it was running
stay unflagged.

For other flex services the picker is vendor-neutral: any `binary_sensor` that is `on` while
the service controls your battery works, including a
[template sensor](https://www.home-assistant.io/integrations/template/) derived from whatever
state sensor your provider's integration offers.

**How far back flagging reaches.** Your energy data (battery, grid, solar) is backfilled a full year regardless of this setting. The *flag* is different: it comes from Home Assistant's recorder **state history**, whose retention is governed by `recorder.purge_keep_days` (10 days by default) — far shorter than the long-term statistics your energy data is read from.

Beyond that retention window there are no recorded state changes, so Home Assistant answers with the last state it still knows about and holds it forward. Which way that falls depends on what that state was. If it was `off` — the usual case for a sensor that spends most of its time off — nothing older than the retention window is flagged, and those energy rows upload unflagged even if the flex service was in fact active back then. If it happened to be `on`, the flag is held forward from there until the next recorded change, which can reach much further back than `purge_keep_days`.

Either way, the flag only ever *marks* intervals that were going to be uploaded anyway — it never creates one. An interval is uploaded because your **battery** sensors have recorded statistics covering it (hourly for older data, which is split across that hour's four quarters; 5-minute for recent data), and the flag is then applied to it. So a stale `on` state cannot turn a stretch where the recorder has nothing — while Home Assistant was down, or before the battery was installed — into a wall of empty "externally controlled" intervals. From the first upload onwards, flagging keeps pace with every new upload as long as the sensor stays configured.

**Known limitation: standby/reserve periods.** Flex-market programs typically hold a slice of capacity in reserve between active dispatch sessions, ready to call on the battery again. If your binary sensor only reports `on` during an actual dispatch — not during the standing-by periods around it — those idle stretches are **not** flagged, even though the reserved capacity was unavailable for your own price optimisation and still affects the grade. There is no way to tell "resting because the price is bad" apart from "resting because a flex program has it reserved" unless the sensor itself reflects the reservation.

**A practical pattern:** most flex-market services expose their own status entity (a text/enum sensor whose value is something like `GridRewardDelivering` while dispatching). Rather than hunting for a ready-made `binary_sensor`, create a [template binary sensor](https://www.home-assistant.io/integrations/template/) derived from it and point the picker there, for example:

```yaml
template:
  - binary_sensor:
      - name: "Flex control active"
        state: "{{ states('sensor.your_control_status_sensor') == 'GridRewardDelivering' }}"
```

Adjust the source entity and state value to whatever your own integration exposes. If that same source entity also distinguishes standby/reserve from active dispatch, folding those states into the template narrows the limitation above.

## What the flex participation paid you

Excluding the flagged intervals keeps the grade fair, but it says nothing about the other half of the trade: the compensation the service pays you for that control. Wolta can show the two side by side — what the participation earned, next to what it cost you in foregone spot value — but only if it knows the amounts.

**The picker.** The same two places (setup flow's entity step and **Reconfigure**) offer an optional **Flex compensation, monthly** field. Point it at a sensor holding the compensation in SEK — CheckWatt, Tibber Grid Rewards and similar services usually expose one, and a [template sensor](https://www.home-assistant.io/integrations/template/) works just as well. Wolta reads the monthly total from long-term statistics rather than from the current state, so a sensor that resets at the start of each month is handled correctly.

**Which sensor to pick.** Two integrations already expose something suitable:

- **CheckWatt** — [`faanskit/ha-checkwatt`](https://github.com/faanskit/ha-checkwatt) publishes *Daily Net Income* and *Annual Net Income* (net after CheckWatt's and the installer's shares, in SEK). Either works: Wolta reads the *increase over the month*, so a daily sensor that resets every day and a yearly one that resets every year both add up to the same monthly figure.
- **Tibber Grid Rewards** — the reward for the current month comes from Tibber's app API; [`JohNan/homeassistant-tibber_grid_rewards`](https://github.com/JohNan/homeassistant-tibber_grid_rewards) exposes it as *Grid Reward Current Month*, which is exactly the figure Wolta wants.

Whichever you pick, open **Developer tools → Statistics** and confirm the entity is listed there with a *sum* — that is the check that matters, not what the integration is called. A sensor missing from that list, or listed without a sum, is the `state_class` problem below.

**The sensor must have `state_class: total` or `total_increasing`.** "Has long-term statistics" is not enough: a `state_class: measurement` sensor gets long-term statistics too, but only min/mean/max — there is no monthly sum to read, and Wolta would have nothing to send. If the sensor you pick yields nothing for about a day, a repair notice appears telling you so; it disappears on its own once a figure comes through or you clear the field. Wolta never invents a zero for a month it could not measure — showing "0 kr compensation" beside a real foregone-spot cost would make flex participation look like a pure loss on a number nobody measured. A month whose total comes out NEGATIVE is skipped for a different reason: Wolta stores compensation of zero or more and refuses the whole update otherwise, so sending one would stop every other month from getting through.

Once a cycle the integration reads the current month and the previous one and sends those two figures on. Last month is re-sent every cycle on purpose: aggregators often settle a month days after it ended, and an estimate that was frozen on first reading would stay wrong.

**Only the current and previous month are read.** Home Assistant keeps long-term statistics indefinitely — they survive `purge_keep_days`, which only trims state history and the short-term statistics — but they start the day your compensation sensor started recording them. Wolta therefore reads two months and no further back: everything before that (and anything from before you installed the sensor) has to be entered by hand in the compensation card on wolta.se, where it is kept as a `manual` amount.

**It never touches what you entered yourself — and the card never touches its months.** Amounts you type into the compensation card on wolta.se are kept separately from the ones the sensor reports, and the integration only ever writes its own. The separation runs both ways: the card lists the sensor's months but cannot change them, so those rows have both fields locked and no Remove button. Clearing the picker stops the reading; the months already recorded stay where they are, and deleting the plant on wolta.se is the only thing that removes them.

Leaving the field empty — the default — changes nothing: no extra sensor is read and nothing is sent.

## What the figures do, and what they never do

**The amounts never move your payback time or IRR.** They are shown next to the foregone spot value so you can see both halves of the flex trade, and that is all they do. Wolta's economy figures are deliberately computed without them: the compensation is paid outside the electricity bill, on a contract you can leave at any time, and reported per plant by the aggregator rather than measured by Wolta. Folding it into the return on the battery would make the investment case depend on a number nobody here can verify. The optimisation grade doesn't use them either — that side of the trade is handled by neutralising the flagged intervals, described above.

**On the grade sensor: the `flex_compensation` attribute (v0.34.0+).** When the backend has something to say about compensation, `sensor.wolta_optimisation_grade` carries it as a structured attribute so you can build your own cards and automations on the figures:

| Key | Meaning |
| --- | --- |
| `compensation_period_sek` | What the recorded amounts add up to over the grade's period. `null` when no amounts overlap it. |
| `compensation_coverage` | The share of the period those amounts actually cover — one entered CheckWatt month against a three-year window is `0.03`, not a small payout. |
| `estimate_period_sek` | `{low, high}`: what participation would plausibly have paid over the same period, from the aggregator's capacity prices and your bid size. A band, not a prediction. |
| `estimate_coverage` | The share of the period the estimate could be computed for (missing price hours lower it). |
| `source` | `manual` (you entered it on wolta.se), `sensor` (read from the picker above) or `estimate` (nothing recorded — the band is all there is). |
| `aggregator` | The flex provider the estimate assumes. |
| `bid_kw` | The bid size used, either yours or one derived from the battery's nameplate figures. |

**Read the two coverage ratios before comparing the two amounts.** They have different bases on purpose: the estimate spans the whole grade window, the recorded amount only the months that exist. Putting a one-month payout next to a three-year estimate as if they were the same quantity is the mistake the ratios are there to prevent. The whole block is absent — not `null` — when there is nothing to say: no amounts, no aggregator, no bid, or a grade computed before this feature shipped.

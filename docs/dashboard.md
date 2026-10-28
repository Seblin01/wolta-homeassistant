# Example dashboard

A ready-made Lovelace view for the Wolta sensors is available in `dashboards/wolta.yaml` (English entity IDs and labels — the default for most instances). A Swedish variant with Swedish entity IDs and labels is available in `dashboards/wolta.sv.yaml`.

## Using the dashboard

1. Open `dashboards/wolta.yaml` and copy the entire contents.
2. In Home Assistant go to **Settings → Dashboards**.
3. Create a new dashboard (type: Lovelace) and choose **Edit manually**.
4. Paste the YAML content and save.

## Verify entity IDs

Entity names follow your Home Assistant language (English and Swedish translations are bundled; other languages fall back to English). HA generates entity IDs from the translated names at install time. The dashboard YAML uses the **English** defaults; on a **Swedish** HA instance, replace them as follows:

| English ID (used in the YAML) | Swedish instance ID |
|-------------------------------|---------------------|
| `sensor.wolta_optimisation_grade` | `sensor.wolta_optimeringsbetyg` |
| `sensor.wolta_comparison_grade` | `sensor.wolta_jamforelsebetyg` |
| `sensor.wolta_battery_value_per_year` | `sensor.wolta_batterivarde_per_ar` |
| `sensor.wolta_internal_rate_of_return_irr` | `sensor.wolta_intern_avkastning_irr` |
| `sensor.wolta_payback_time` | `sensor.wolta_aterbetalningstid` |
| `sensor.wolta_actual_savings_this_year` | `sensor.wolta_facit_i_ar` |
| `sensor.wolta_data_status` | `sensor.wolta_datastatus` |
| `sensor.wolta_spot_price` | `sensor.wolta_spotpris` |
| `sensor.wolta_spot_price_with_forecast` | `sensor.wolta_spotpris_med_prognos` |
| `sensor.wolta_purchase_price` | `sensor.wolta_inkopspris` |
| `sensor.wolta_sale_price` | `sensor.wolta_forsaljningspris` |
| `sensor.wolta_purchase_price_with_forecast` | `sensor.wolta_inkopspris_med_prognos` |
| `sensor.wolta_sale_price_with_forecast` | `sensor.wolta_forsaljningspris_med_prognos` |
| `sensor.wolta_status` | `sensor.wolta_status` |
| `button.wolta_recompute` | `button.wolta_rakna_om` |

If your entities differ (other language, integrations installed before v0.4.3, or a `_2` suffix when installed more than once): go to **Settings → Devices & Services → Wolta** to see the exact IDs, and adjust the dashboard YAML accordingly.

## Notes

The dashboard ends with a markdown card linking to your full results on wolta.se. The link is resolved dynamically from the device's `configuration_url` (v0.4.1+; a read-scoped link since v0.30.0 — see [Full results on wolta.se](configuration.md#full-results-on-woltase)), so no manual token pasting and no dashboard change are needed.

# Feeding the prices to EMHASS

A template sensor can turn the `prices` attribute into a plain list, for example for EMHASS's `load_cost_forecast`. This is an illustration, not a tested configuration: adapt the horizon (here 96 quarters, 24 hours) and the time step to your EMHASS setup. The prices in the attribute are excluding VAT; the template already multiplies by `vat_factor`, so the list is VAT-inclusive (drop that factor for the raw prices). Grid tariffs and other taxes are not included (the [*Purchase price* sensor](sensors.md#purchase-price-sensor) has them, including VAT). Points without a price (no exchange rate) are left out, which can shorten or shift the list, and EMHASS needs a list that matches its horizon: check the length before relying on it. Since v0.52.0 the `prices` list can run well past 96 quarters (up to about three days of forecast), so keep the slice at the end of the template, or the list becomes longer than the EMHASS horizon; the days beyond tomorrow are the least certain part of it.

```yaml
template:
  - sensor:
      - name: "Spot price forecast list"
        unique_id: wolta_spot_price_forecast_list
        state: "{{ (state_attr('sensor.wolta_spot_price_with_forecast', 'prices') or []) | length }}"
        attributes:
          load_cost_forecast: >-
            {% set vat = state_attr('sensor.wolta_spot_price_with_forecast', 'vat_factor') | float(1.25) %}
            {% set points = state_attr('sensor.wolta_spot_price_with_forecast', 'prices') or [] %}
            {% set from_ts = now() - timedelta(minutes=15) %}
            {% set ns = namespace(prices=[]) %}
            {% for p in points %}
              {% if p.price is not none and as_datetime(p.start) >= from_ts %}
                {% set ns.prices = ns.prices + [(p.price * vat) | round(4)] %}
              {% endif %}
            {% endfor %}
            {{ ns.prices[:96] }}
```

Replace `sensor.wolta_spot_price_with_forecast` with your entity ID (see [Verify entity IDs](dashboard.md#verify-entity-ids)). The last quarters of the list are forecast values; check `forecast` on the points if you want to treat them differently. The sensor's state is the number of points (a stable value on purpose; a timestamp would be recorded every minute). The list changes every quarter-hour, so consider excluding the sensor from the recorder:

```yaml
recorder:
  exclude:
    entities:
      - sensor.spot_price_forecast_list
```

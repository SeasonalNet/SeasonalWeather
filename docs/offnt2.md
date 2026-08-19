# OFFNT2 offshore forecast

SeasonalWeather's `offnt2` segment reads the Mid-Atlantic Offshore Waters
Forecast from the Ocean Prediction Center.  Acquisition may use the `OFF`
product index at `KWBC`, but the product is accepted only when its content
identifies `OFFNT2` or `FZNT22 KWBC`; an unexpected offshore region is rejected
before it reaches the broadcast builder.

The segment is disabled by default.  To enable it, configure `cycle.offnt2`
with one or more `ANZ` zones and human-readable labels:

```yaml
cycle:
  offnt2:
    enabled: true
    zones:
      - {id: "ANZ450", label: "New Jersey waters"}
      - {id: "ANZ452", label: "Delaware and Virginia waters"}
```

Configured zones rotate deterministically using `rotate_period_s` and
`rotate_step`.  The renderer applies both character and estimated airtime
budgets, while warning headlines remain protected from ordinary body-budget
trimming.  The optional synopsis is omitted in heightened mode when routine
offshore content is deferred; a warning-bearing zone remains eligible.

The OFFNT2 builder is an independent registry target.  Refreshing `offnt2`
uses the P1-20 one-target refresh path and does not regenerate CWF, forecasts,
or the rest of the cycle.  When the compatibility full-cycle composer has
already built CWF, an equivalent OFF synopsis is not repeated in the OFFNT2
segment.

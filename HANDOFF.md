# EU Data Act — Development Handoff

**Branch:** `feat/eu-data-act`  
**Last updated:** 2026-06-04  
**Target repo:** `Donsig/homeassistant-volkswagencarnet`

---

## Goal

Add the VW Group EU Data Act portal (`eu-data-act.drivesomethinggreater.com`) as an alternative data source for the integration. The old We Connect API was closed by VW. Under EU law, VW must expose vehicle data via this portal. The legacy path is left untouched.

**Plan file:** `docs/superpowers/plans/2026-06-04-eu-data-act.md`

---

## Progress (7 of 10 tasks complete)

| # | Task | Commit | Status |
|---|------|--------|--------|
| 1 | Add EU Data Act constants to `const.py` | `15a34ac` | ✅ |
| 2 | `eu_data_act.py` — HTML login helpers + tests | `d5d6163` | ✅ |
| 3 | Dataset parsing (`_parse_dataset`, `_newest_dataset`) + tests | `f93148c` | ✅ |
| 4 | Login & data-fetch methods (`login`, `vehicles`, `status`) | `bfda30d` | ✅ |
| 5 | Coordinator + entity classes | `37e1ee8` | ✅ |
| 6 | Entity value unit tests (26 total) | `e22e6ee` | ✅ |
| 7 | Wire `__init__.py` — setup/unload branching | `d31257e` | ✅ |
| 8 | Wire `sensor.py` + `binary_sensor.py` | — | ⏳ |
| 9 | Config flow EU Data Act path + strings | — | ⏳ |
| 10 | Push branch + open PR on Donsig fork | — | ⏳ |

**Tests:** `pytest tests/ -v --timeout=30` → 29 passing

---

## Task 8 — Wire sensor.py and binary_sensor.py

### sensor.py

Read `custom_components/volkswagencarnet/sensor.py` first, then replace `async_setup_entry`:

```python
async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Set up the entity."""
    from .const import CONF_DATA_SOURCE, DATA_SOURCE_EU_DATA_ACT, EUDA_COORDINATOR, DOMAIN, CONF_VEHICLE
    from .eu_data_act import EU_SENSORS, EUDataActSensor

    if entry.data.get(CONF_DATA_SOURCE) == DATA_SOURCE_EU_DATA_ACT:
        coordinator = hass.data[DOMAIN][entry.entry_id][EUDA_COORDINATOR]
        vin = entry.data[CONF_VEHICLE].upper()
        async_add_devices([EUDataActSensor(coordinator, vin, d) for d in EU_SENSORS])
        return True

    data = hass.data[DOMAIN][entry.entry_id][DATA]
    coordinator = data.coordinator
    async_setup_platform_entities(
        hass, entry, coordinator, data, "sensor", VolkswagenSensor, async_add_devices
    )
    return True
```

Note: `CONF_VEHICLE` is already in `.const` (value: `"vehicle"`). The top-level import of `DATA` and `DATA_KEY` stays.

### binary_sensor.py

Replace `async_setup_entry`:

```python
async def async_setup_entry(hass: HomeAssistant, entry, async_add_devices):
    """Set up the Volkswagen binary sensor."""
    from .const import CONF_DATA_SOURCE, DATA_SOURCE_EU_DATA_ACT, EUDA_COORDINATOR, DOMAIN, CONF_VEHICLE
    from .eu_data_act import EU_BINARY_SENSORS, EUDataActBinarySensor

    if entry.data.get(CONF_DATA_SOURCE) == DATA_SOURCE_EU_DATA_ACT:
        coordinator = hass.data[DOMAIN][entry.entry_id][EUDA_COORDINATOR]
        vin = entry.data[CONF_VEHICLE].upper()
        async_add_devices([EUDataActBinarySensor(coordinator, vin, d) for d in EU_BINARY_SENSORS])
        return True

    data = hass.data[DOMAIN][entry.entry_id][DATA]
    coordinator = data.coordinator
    async_setup_platform_entities(
        hass,
        entry,
        coordinator,
        data,
        "binary_sensor",
        VolkswagenBinarySensor,
        async_add_devices,
    )
    return True
```

**Verify:** `pytest tests/ -v --timeout=30` → all pass  
**Commit:** `git commit -m "feat: sensor/binary_sensor dispatch for EU Data Act"`

---

## Task 9 — Config flow + strings

### config_flow.py changes

1. **Add imports** (merge into existing `from .const import` and add new block):
```python
from .const import (
    # ... existing ...
    CONF_DATA_SOURCE,
    DATA_SOURCE_LEGACY,
    DATA_SOURCE_EU_DATA_ACT,
    CONF_BRAND,
)
from .eu_data_act import (
    EUDataActAPI,
    BRANDS as EUDA_BRANDS,
    ConsentRequired,
    LoginError,
)
```

2. **Expand `DATA_SCHEMA`** — add `CONF_DATA_SOURCE` (default `eu_data_act`) and `CONF_BRAND` (default `"Volkswagen"`) fields before `CONF_SPIN`.

3. **Branch in `async_step_user`** after `user_input is not None`:
```python
data_source = user_input.get(CONF_DATA_SOURCE, DATA_SOURCE_LEGACY)
if data_source == DATA_SOURCE_EU_DATA_ACT:
    return await self.async_step_eu_login()
# ... existing legacy path ...
```

4. **Add three methods** to `VolkswagenCarnetConfigFlow`:
   - `_async_task_eu_login()` — creates API, logs in, discovers vehicles; always calls `async_configure` at end (even on error)
   - `async_step_eu_login()` — progress step using `progress_action="task_eu_login"`
   - `async_step_eu_select_vehicle()` — vehicle picker → `async_create_entry`

See full code in `docs/superpowers/plans/2026-06-04-eu-data-act.md` Task 9 Steps 3–4.

### strings.json + translations/en.json

Add to `"config"."step"."user"."data"`:
```json
"data_source": "Data source",
"brand": "Brand (for EU Data Act)"
```

Add to `"config"."step"`:
```json
"eu_login": {
  "title": "EU Data Act Portal",
  "description": "Signing in to the EU Data Act portal..."
},
"eu_select_vehicle": {
  "title": "Select Vehicle",
  "description": "The following vehicles were found on the EU Data Act portal.",
  "data": { "vehicle": "Vehicle" }
}
```

Add to `"config"."error"`:
```json
"eu_consent_required": "Portal consent not yet completed. Open https://eu-data-act.drivesomethinggreater.com in a browser, log in, accept the consent screen, link your vehicle and enable the continuous data request (15 min interval)."
```

Add to `"config"."abort"`:
```json
"no_vehicles_found": "No vehicles found on the EU Data Act portal. Complete the one-time portal setup first."
```

Add to `"config"."progress"`:
```json
"task_eu_login": "Signing in to the EU Data Act portal"
```

**Verify:** `pytest tests/ -v --timeout=30` → all pass  
**Commit:** `git commit -m "feat: EU Data Act config flow path (eu_login + eu_select_vehicle steps)"`

---

## Task 10 — Push + PR

```bash
pytest tests/ -v --timeout=30   # final check
git push -u origin feat/eu-data-act
gh pr create \
  --repo Donsig/homeassistant-volkswagencarnet \
  --base master \
  --title "feat: add EU Data Act portal as alternative data source" \
  --body "..."   # see plan Task 10 for full body
```

---

## Key design decisions

- **Legacy path untouched** — existing entries continue to work unchanged
- **No GPS** — portal doesn't provide position data; only 5 sensors + 2 binary sensors
- **15-min minimum poll interval** — portal publishes snapshots every ~15 min
- **One-time browser consent required** — integration detects missing consent and shows `eu_consent_required` error
- **Personal fork only** — not contributing upstream; lives at `Donsig/homeassistant-volkswagencarnet`

## Reference implementations

- evcc Go: `vehicle/vw/eudataact/` in https://github.com/evcc-io/evcc (PR #30362)
- ioBroker JS: `lib/euDataAct.js` in https://github.com/TA2k/ioBroker.vw-connect

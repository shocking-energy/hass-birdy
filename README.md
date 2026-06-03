# Birdy — Home Assistant integration

Home Assistant integration for **Birdy**, the AI energy dashboard for GivEnergy
(powered by Shocking Energy).

This is Birdy's local-first route into Home Assistant. HA talks to your GivEnergy
inverter directly over your local network — **GivEnergy Cloud is not required** —
sends your system's live data up to your Birdy account, and adds **43 entities** to
HA (13 read-only sensors, 16 inverter controls, 6 battery diagnostics,
2 forecast values, 6 status/diagnostic entities) for use in dashboards and
automations.

## Two install modes

Since 0.11.0 Birdy supports two install modes — pick at first-run:

- **Cloud-attached (default)** — Birdy syncs your data to a Shocking Energy
  account. Unlocks the cloud dashboard at <https://app.shocking.energy>, the
  Birdy AI assistant, today's solar forecast + expected-daily-house values
  for automations, and multi-device data sharing (Pi + HA + phone seeing the
  same numbers).
- **Local mode** — tick the *Local mode* box on the intro screen. No account,
  no telemetry leaves your LAN. You get the same 41 Home Assistant entities
  (13 read sensors, 16 inverter controls, 6 battery diagnostics, 6 status
  diagnostics) and full local control. You don't get the cloud dashboard, the
  AI assistant, or the 2 forecast values. Swap to cloud mode later by
  re-adding the integration.

## Requirements

- Home Assistant **2023.11.0** or newer (uses the `time` entity).
- LAN access from HA to the GivEnergy dongle on TCP **8899** (auto-discovered, or
  set manually).
- HA host clock within ±5 min of UTC — Birdy rejects readings whose timestamps
  are too far off. *Local mode is exempt from this requirement.*
- **Cloud mode only**: a Birdy account at <https://app.shocking.energy>.

## Install

### HACS (recommended)

1. HACS — search **Birdy** — Download. *(Until accepted to the default list:
   HACS — ⋮ — Custom repositories — add
   `https://github.com/shocking-energy/hass-birdy` as category **Integration**.)*
2. Restart HA.
3. Settings — Devices & services — **Add Integration** — **Birdy**.

## First run — cloud mode (default)

1. Add the integration and click **Submit** (leave *Local mode* unchecked).
2. It scans the LAN for the dongle; if none is found, it asks for the inverter IP.
3. Open <https://app.shocking.energy> **from a device on the same network as HA**
   and click **Adopt** on the banner. This links the integration to your account.
4. Entities populate within ~10 s.

**If adoption doesn't complete:** linking works automatically when the browser you
open the dashboard in and your HA server are on the same internet connection. If
they're not — e.g. you're on mobile data, a VPN, or a CGNAT broadband setup — the
automatic link stops after 2 minutes. For now, open the dashboard from a device on
the same network as HA. A manual code option is planned.

## First run — local mode

1. Add the integration → tick **Local mode (no Shocking Energy account)** →
   click **Submit**.
2. It scans the LAN for the dongle; if none is found, it asks for the inverter IP.
3. Entities populate within ~10 s. No browser step, no banner — Birdy is
   already linked to your inverter.

Role diagnostic reads `local`; Account ID reads `local install`. The two
forecast sensors stay `unavailable` because no cloud is computing them.

## Entities

- **Read sensors (13)** — live grid / battery / solar / house power, plus
  today's energy totals. The energy sensors are tagged
  `device_class: energy` + `state_class: total_increasing`, so they drop straight
  into HA's Energy dashboard.
- **Inverter controls (16)** — eco mode, AC charge / DC discharge schedules,
  reserve / cutoff, charge / discharge rates, AC charge limit, max output %. All
  changes go straight to the inverter over your local network (no cloud
  round-trip).
- **Battery diagnostics (6)** — state of health, total capacity (kWh), calibrated
  and design capacity (Ah), pack voltage, pack temperature. Read off the BMS
  every poll cycle.
- **Forecast (2)** — today's expected solar (computed in the cloud from
  Open-Meteo + your install's learning factors) and your stated daily house
  consumption target. These feed the kiosk's `actual / expected` displays and are
  useful triggers for automations ("if tomorrow's expected solar is low, AC-charge
  to 80% tonight").
- **Diagnostics (6)** — role (master/monitor), last-published time, integration
  version, account ID, connected, publishing.

### Disabling controls

To remove all the control entities (switches/numbers/times) but keep the read
sensors + diagnostics, set this on the HA host and restart:

```
BIRDY_HA_CONTROLS_ENABLED=false
```

## Dashboard

A ready-made Lovelace dashboard is included at
[`dashboards/birdy-home.yaml`](dashboards/birdy-home.yaml) — live power flow,
energy distribution, battery/charge/discharge controls, and tiles for every
sensor.

It needs the **Power Flow Card Plus** custom card (HACS → Frontend → search
"Power Flow Card Plus") for the live flow diagram, and HA's Energy dashboard
configured for the energy-distribution card. To use it: Settings → Dashboards →
add a dashboard → open it → ⋮ → Edit → ⋮ → Raw configuration editor → paste the
file's contents.

## Master and monitor

Birdy lets only **one device per account send data at a time** — the *master*. Any
others are *monitors*.

- If another device (e.g. a Pi) is already the master, HA installs as a monitor:
  it won't send data, and instead displays the live figures it reads back from
  Birdy.
- To make HA the master, demote the existing one at <https://admin.shocking.energy>,
  then reload the integration.

HA's current role is shown in its diagnostic entities.

## Security

- Each account is private — this install can only ever read and write your own
  data.
- HA stores a login token for Birdy in its config entry (standard HA behaviour;
  held unencrypted in `.storage/core.config_entries`). The token only lets this
  install publish its own data.
- Inverter controls are sent directly over your local network. Anything on that
  network can reach the inverter, so **securing your LAN is your responsibility**.

## Reporting issues

<https://github.com/shocking-energy/hass-birdy/issues> — include HA Core
version + install type, GivEnergy model, integration version
(`sensor.birdy_ha_integration_version`), whether controls are enabled, and logs
filtered to `custom_components.birdy_ha`.

---

Powered by Shocking Energy · Maintenance by TEMS Solar

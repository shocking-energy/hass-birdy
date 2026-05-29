# Birdy — Home Assistant integration

Home Assistant integration for **Birdy**, the AI energy dashboard for GivEnergy
(powered by Shocking Energy).

This is Birdy's local-first route into Home Assistant. HA talks to your GivEnergy
inverter directly over your local network — **GivEnergy Cloud is not required** —
sends your system's live data up to your Birdy account, and adds **43 entities** to
HA (13 read-only sensors, 16 inverter controls, 6 battery diagnostics,
2 forecast values, 6 status/diagnostic entities) for use in dashboards and
automations.

## Requirements

- Home Assistant **2023.11.0** or newer (uses the `time` entity).
- LAN access from HA to the GivEnergy dongle on TCP **8899** (auto-discovered, or
  set manually).
- HA host clock within ±5 min of UTC — Birdy rejects readings whose timestamps
  are too far off.
- A Birdy account at <https://app.shocking.energy>.

## Install

### HACS (recommended)

1. HACS — search **Birdy** — Download. *(Until accepted to the default list:
   HACS — ⋮ — Custom repositories — add
   `https://github.com/shocking-energy/hass-birdy` as category **Integration**.)*
2. Restart HA.
3. Settings — Devices & services — **Add Integration** — **Birdy**.

## First run

1. Add the integration and click **Submit** (no fields).
2. It scans the LAN for the dongle; if none is found, it asks for the inverter IP.
3. Open <https://app.shocking.energy> **from a device on the same network as HA**
   and click **Adopt** on the banner. This links the integration to your account.
4. Entities populate within ~10 s.

**If adoption doesn't complete:** linking works automatically when the browser you
open the dashboard in and your HA server are on the same internet connection. If
they're not — e.g. you're on mobile data, a VPN, or a CGNAT broadband setup — the
automatic link stops after 2 minutes. For now, open the dashboard from a device on
the same network as HA. A manual code option is planned.

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

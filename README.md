<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/logo-dark.png">
    <img src="images/logo-light.png" alt="SplashMe" width="320">
  </picture>
</p>

<h1 align="center">SplashMe for Home Assistant</h1>

<p align="center"><strong>Smart. Simple. Connected.</strong></p>

<p align="center">
  <a href="https://github.com/laboratoryyingong/splashme-home-assistant/releases"><img src="https://img.shields.io/github/v/release/laboratoryyingong/splashme-home-assistant?include_prereleases&label=release" alt="Latest release"></a>
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-custom%20repository-41BDF5" alt="HACS custom repository"></a>
  <a href="https://www.splashmepool.com.au/"><img src="https://img.shields.io/badge/splashmepool.com.au-002b66" alt="SplashMe website"></a>
</p>

Control your SplashMe pool controller from Home Assistant over your local
network. Sign in once with your SplashMe account; after that every reading and
every command goes directly between Home Assistant and the controller, with no
cloud round trip.

## About SplashMe

[SplashMe](https://www.splashmepool.com.au/) is an Australian-owned, locally
manufactured pool automation system. The Automation Controller runs
filtration, dosing, heating, lighting, cleaners and water features from one
place; the Sensor & Dosing Module keeps water chemistry and temperature in
check; the Power Xpander and Poolside Switch add more equipment and a control
point by the pool. It installs alongside existing equipment, works with the
major pool equipment brands and receives over-the-air updates.

This integration brings the same controller into Home Assistant, so the pool
can take part in your automations and dashboards next to the rest of your
home.

> **Early access.** This integration is in active testing and changes often.
> Local control needs a controller firmware that is rolled out on request:
> email **support@splashmepool.com.au** with your controller's device ID
> (shown in the SplashMe app) and we will upgrade it for you. Please report
> anything that does not work through the
> [issue tracker](https://github.com/laboratoryyingong/splashme-home-assistant/issues).

## What you get

- **Water chemistry**: pH, ORP, water and ambient temperature, pressure,
  doses today, drum volumes and chemical remaining, desired pH / ORP setpoints.
- **Pump**: on/off, speed, mode, start-up status (starting, priming with a
  countdown), flow rate, brand.
- **Heater** and **auxiliary outputs** (lights and other relays).
- **Schedules**: `splashme.create_schedule`, `splashme.update_schedule`,
  `splashme.delete_schedule` services to manage the controller's pool schedules.
- **SplashMe dashboard**: a ready-made sidebar panel with tank, heater and
  chemistry cards, generated for each controller from the equipment it has,
  and a Trends tab with the history graphs.
- Diagnostics download for support.

## Requirements

- Home Assistant 2025.1 or newer.
- A SplashMe account with at least one paired controller.
- Controller firmware **2.5.45 or newer** (see the early access note above).
- The controller and Home Assistant on the same local network. The controller
  announces itself with mDNS; it must be reachable on TCP port 8080.

## Installation

### HACS (recommended)

1. In HACS open **Integrations**, then the three-dot menu, then
   **Custom repositories**.
2. Add `https://github.com/laboratoryyingong/splashme-home-assistant` with
   category **Integration**.
3. Search for **SplashMe** in HACS, install it, and restart Home Assistant.

Updates arrive through HACS like any other integration. When we publish a new
release, HACS shows an update; install it and restart Home Assistant.

### Manual

Copy `custom_components/splashme` into the `custom_components` folder of your
Home Assistant configuration directory and restart Home Assistant.

## Setup

1. Go to **Settings → Devices & services → Add integration** and pick
   **SplashMe**.
2. You are sent to the SplashMe sign-in page. Sign in with the same account
   you use in the SplashMe app.
3. The integration scans the local network for every controller paired to
   your account and adds the ones it finds. Controllers it cannot find are
   listed; make sure they are powered and on the same network, then choose
   **Scan again**, or finish and they are added the next time they are seen.

Signing in is needed once. The SplashMe cloud is only used during setup to
confirm which controllers belong to you; afterwards the integration talks to
the controller directly.

## Troubleshooting

**After signing in, the browser lands on a page that cannot be reached
(for example `localhost:8123/auth/external/callback?...`).**
The sign-in page hands you back to Home Assistant through
my.home-assistant.io, which uses the Home Assistant address saved in your
browser. Open <https://my.home-assistant.io/>, set the Home Assistant URL to
the address you normally use (such as `http://homeassistant.local:8123` or
your Home Assistant's IP address with port 8123), save, then add the
integration again. To rescue the current attempt instead, edit the address
in the browser bar: replace the host with your Home Assistant address and
keep everything after `/auth/external/callback` unchanged.

**The scan finds 0 of N paired devices.**
The controller must be powered on, on the same network as Home Assistant,
and running firmware 2.5.45 or newer. Multicast DNS must be able to cross
between Home Assistant and the controller, so a Wi-Fi guest network or VLAN
in between will block discovery. Choose **Scan again** once fixed, or finish:
the controller is added automatically the next time it is seen.

**"Your SplashMe account is not paired with this device".**
Only controllers paired to the account you signed in with can be added.
Sign in with the account that owns the controller in the SplashMe app.

**pH, ORP and water temperature don't change while the pump is off.**
This is expected. Without water flow the probes sit in still water, so these
sensors keep the last reading taken while the pump was running, and update
again once it has been running for about 2 minutes (**Chemistry Stable** turns
on). Flow rate and pressure stay live, so they show the pump is off. Right
after installing, the held sensors read "unknown" until the pump has run once.

**The dashboard has no heater dial.**
The target temperature dial appears when the heater's output is set up as
Pool Heater, Spa Heater or Solar in the SplashMe app: the controller then runs
it to the target temperature. A heater on a general auxiliary output is only
switched on and off. Give that output a name containing "heat" (such as "Heat
Pump"), in the SplashMe app or by renaming its switch in Home Assistant, and
the dashboard shows its switch under Heating.

**The Pool dashboard doesn't pick up changes.**
Once you edit the Pool dashboard, the integration leaves it alone, so new
equipment and dashboard improvements no longer appear. Press **Reset Pool
Dashboard** (**Settings → Devices & services → SplashMe →** your controller,
under Configuration) to go back to the generated layout; your edits are lost.

## Getting help

- Something not working: open an
  [issue](https://github.com/laboratoryyingong/splashme-home-assistant/issues)
  and attach the diagnostics file (**Settings → Devices & services →
  SplashMe → three-dot menu → Download diagnostics**).
- Firmware upgrades and account questions: support@splashmepool.com.au.

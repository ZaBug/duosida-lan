# Duosida LAN

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2025.8%2B-blue.svg)](https://www.home-assistant.io/)

Local control of **Duosida EV wallboxes** from Home Assistant, over your own
network. No cloud account, no polling of a vendor server: the integration talks
to the wallbox on TCP port `9988`, the same local protocol the vendor app uses.

> **Status: alpha.** Tested on one DUOSIDA Mode3@32A (single phase, UCHEN
> firmware `V2.5`). Other Duosida / UCHEN models may work. Please report
> what works on yours.

## Features

| Entity | What it does |
|---|---|
| `sensor.*_status` | Available, Preparing, Charging, Suspended by vehicle / wallbox, Finishing, Faulted… |
| `sensor.*_current`, `_voltage`, `_power` | Live measurements from the wallbox |
| `sensor.*_session_energy` | Energy of the running session (kWh) |
| `sensor.*_energy_since_restart` | Wallbox energy register; restarts after a power cycle (`total_increasing`) |
| `sensor.*_temperature`, `_error`, `_max_current_setting` | Diagnostics |
| `number.*_max_current` | Maximum charging current, 6 A up to the wallbox rating. Applied live, also while charging. The state is the value **read back** from the wallbox |
| `switch.*_charging` | On while a session runs. Turn on = remote start, off = remote stop |
| `button.*_start_charging`, `_stop_charging` | Explicit start / stop |
| `button.*_start_stop_charging` | Toggle: stops a running session, otherwise starts one |
| `button.*_refresh` | Request status, meter values and settings now |

Verified live on the test unit: reading status, meters and settings; setting the
current (6 → 10 A while charging took effect in under 30 s); remote stop
(current to 0 within 1 s); remote start (6 A within 5 s).

## Installation

1. HACS → Integrations → ⋮ → **Custom repositories** → add
   `https://github.com/ZaBug/duosida-lan`, category **Integration**.
2. Install **Duosida LAN** and restart Home Assistant.
3. Settings → Devices & services → **Add integration** → *Duosida LAN*.
   The integration searches the local network and lists the wallboxes that
   answered (IP and MAC address). Pick yours, or choose *Enter the IP address
   manually* (port `9988`) if it is on another subnet or VLAN.

Give the wallbox a static DHCP lease. Close the Duosida app while adding the
integration (see *Connection limits*).

### Changing the address later

If the wallbox gets a new IP address: Settings → Devices & services →
Duosida LAN → ⋮ → **Reconfigure**. Pick the new address from the list or enter
it. The new address is checked over UDP only (the running session is not
disturbed), and the MAC address must match the configured wallbox. The
integration then reconnects, which can take about a minute.

## Options

| Option | Default | Notes |
|---|---|---|
| Poll interval | 15 s | How often status and meter values are requested (10–120 s). Settings are read every 60 s. |

## Using it with solar surplus charging

The `number` and the toggle button follow the same contract as the cloud
integration, so for example [EV Solar Manager](https://github.com/ZaBug/ev-solar-manager)
can drive the wallbox locally:

```yaml
ev_solar_manager:
  target_number: number.duosida_mode3_32a_max_current
  charger_status_entity: sensor.duosida_mode3_32a_status
  charging_state: "charging"
  stopped_state: "finishing"
  charger_start_stop_button: button.duosida_mode3_32a_start_stop_charging
```

(Entity ids depend on the device name; check yours under the device page.)

## How it works

Discovery: the integration sends `smart_chargepile_search` to UDP `48899`
(broadcast, or directly to one address); each wallbox answers with
`ip,mac,type,firmware`. Discovery opens no TCP connection.

The wallbox speaks **OCPP 1.6 encoded as Protobuf** over plain TCP. Every
message is one OCPP message type plus the device id (field 100) and a message
id (field 101); answers echo the message id. The integration:

* opens **one persistent session** and sends the same opening message as the
  vendor app (a TriggerMessage for BootNotification); the wallbox answers with
  its identity;
* requests StatusNotification and MeterValues every poll interval, and the
  stored configuration every 60 s;
* sets the current with `ChangeConfiguration` `VendorMaxWorkCurrent`, starts
  and stops with `RemoteStartTransaction` / `RemoteStopTransaction`;
* confirms the transaction messages the wallbox sends to the local session
  (StartTransaction gets a locally generated transaction id), so remote stop
  always has an id to use.

## Connection limits (important)

The wallbox Wi-Fi module is slow (802.11b) and accepts very few local sessions.
What was observed:

* After a session closes, the slot stays busy for about 30 s. The integration
  therefore waits 60 s before reconnecting, then 120 s, then 300 s.
* Reconnecting every few seconds made it refuse every connection for minutes.
* Opening a second session next to a running one once locked the local port
  until the wallbox was power-cycled.

So: do not run other local Duosida tools (or keep the vendor app open on the
same network) next to this integration. The cloud connection of the wallbox is
not affected; cloud-based integrations keep working.

## Known limitations

* While the integration is connected, sessions are confirmed locally. Session
  history in the Duosida app / cloud may be incomplete for those sessions.
* After a Home Assistant restart during a running session, the transaction id
  is learned from the next periodic meter push (up to about a minute); remote
  stop fails with a clear error until then.
* Settings are written to the wallbox flash and survive a power cycle. Avoid
  writing the current more often than needed.
* Not tested: three-phase models, models other than Mode3@32A, RFID / non
  plug-and-charge setups.

## Credits

Protocol knowledge comes from live captures on the test unit and from these
open-source projects, whose work made this possible:

* [matiaskunin/duosida-local](https://github.com/matiaskunin/duosida-local): protocol notes and confidence levels
* [sarpba/duosida-wallbox-addon](https://github.com/sarpba/duosida-wallbox-addon): full OCPP message map
* [americodias/duosida-ev](https://github.com/americodias/duosida-ev): first public TCP client

No code was copied from them. This project is not affiliated with Duosida,
UCHEN or DSCharge.

## License

[MIT](LICENSE)

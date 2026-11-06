<p align="center">
  <img src="custom_components/relay_cover/brand/icon.png" alt="Relay cover icon" width="128">
</p>

# Relay cover

[![Open your Home Assistant instance and open this repository.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=ignaciogallegossanchez&repository=relay-cover&category=integration)
[![Open your Home Assistant instance and start setting up a relay cover.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=relay_cover)

A Home Assistant integration that turns two existing switches into one shutter: one relay
moves it up, the other moves it down. The position is estimated from the time each relay
stays on.

It is set up entirely from the user interface; there is no YAML.

## Requirements

- Home Assistant; tested with 2026.10.4.
- Two `switch` entities that drive the motor in maintained mode: the motor runs while the
  relay is on and stops when it goes off.

## Installation

From Home Assistant 2026.11 on, with the Marketplace (**Settings > Marketplace**); before
2026.11, with HACS. The first badge above opens this repository in either of them.

While Relay cover is not in the catalog, add it as a custom repository first:

1. In the Marketplace (or HACS), add
   `https://github.com/ignaciogallegossanchez/relay-cover` as a custom repository of type
   **Integration** (in HACS: three-dot menu, **Custom repositories**).
2. Install **Relay cover** and restart Home Assistant.

Then add it from **Settings > Devices & services**, or with the second badge above.

By hand: copy `custom_components/relay_cover` into the `custom_components` folder of your
configuration and restart Home Assistant.

## Configuration

**Settings > Devices & services > Helpers > Create helper > Relay cover**.

| Field | Meaning |
|---|---|
| Name | Name of the shutter and of its device. |
| Open relay | Switch that moves the shutter up while it is on. |
| Close relay | Switch that moves the shutter down while it is on. |
| Opening time | Seconds from fully closed to fully open (decimals allowed). |
| Closing time | Seconds from fully open to fully closed (decimals allowed). |
| Software interlock | Never turn both relays on together, and wait 0.5 s after one goes off before turning the other on. Off, the integration never switches a relay because of what the relays do from outside: only orders from Home Assistant switch them. On by default. |
| Extra time at the ends | Percent of the full travel time (0 to 100) that the relay stays on longer when opening or closing fully, so that the shutter really reaches the end. 20 % by default. |
| Hide relays | Hide the two switches so the shutter is used instead. On by default. |

Measure the travel times carefully: the position is only as good as they are.

Everything but the name can be changed later from the helper's **Configure** button; the
helper reloads with the new values. Change them while the shutter is stopped: a reload
while it moves loses the target (see [Reloads and restarts](#reloads-and-restarts)). The
two relays must be different, and a relay can only belong to one relay cover.

## What you see

- Each relay cover gets its own device with one `cover` entity (device class `shutter`).
  The state is marked as assumed: it is an estimate, not a reading.
- A new helper does not know its position yet: it shows unknown and only allows opening
  or closing fully. After one full travel the position is known.
- Opening, closing and going to a position switch the matching relay on, and off again
  after the computed time. The position follows what Home Assistant shows of the relays:
  it starts when the relay is shown on and stops when it is shown off. While the shutter
  moves, its position is updated every second.
- Opening or closing fully runs a little longer than computed (the extra time at the
  ends); meanwhile the shutter shows the end and still opening or closing.
- Wall buttons: when a relay changes outside Home Assistant the shutter follows it. A
  relay switched on from outside is not switched off at the end: the shutter shows the
  end and opening or closing until the relay goes off.
- After a restart or a power cut the last position is restored.

## Details

### Extra time at the ends

An order to an end (open, close, or a position of 0 or 100) keeps the relay on for the time
to that end plus the extra time, a percent of the full travel time of that direction, and
never longer than a full travel. The position never leaves 0-100.

Example with opening 10 s, closing 20 s and 20 %:

- from 90 %, open runs 1 + 2 = 3 s;
- from 90 %, close runs 18 + 4 = 22 s, so 20 s (a full travel);
- already at 0 %, close runs 0 + 4 = 4 s;
- going to 50 % has no extra time.

From the opposite end there is no extra time: from 0 %, open runs 10 s, a full travel. If
the measured opening time is too short, a full opening from closed stops short of the top.

With 0 % the relay goes off when the estimated position reaches the end, and an order to
the end the shutter is believed at does not move it: it stops the shutter if it is moving
away from that end, and changes nothing otherwise. With 100 % every order to an end runs a
full travel. Asking for a position in the middle the shutter is already at stops
it if it is moving, and does nothing otherwise.

The full travel counts from when the motor started, also when a wall button started it.
With extra time, if a wall button has held a relay on for more than a full travel, an
order from Home Assistant to the end of that direction switches the relay off at once.

The estimate drifts a little with each movement. The extra time corrects it each time the
shutter opens or closes fully; with 0 % it is never corrected that way.

### Wall buttons and other changes from outside

- When a relay changes outside Home Assistant, the shutter follows it and the target of
  our order, if any, is given up. A relay switched off from outside stops the shutter.
- A relay switched on from outside (a wall button, another automation) is not switched
  off by the integration on its own, not even at the end. The exceptions:
  - an order from Home Assistant: a stop switches off both relays; an order makes the
    relay of its direction its own, also one held by a wall button, and switches it off
    at its target; with interlock an order in the other direction switches the wall's
    relay off first (and tries again if that off is not shown);
  - with interlock, the other relay switched on from outside (see below).
- A reading glitch of the running relay (off and on again) counts as two presses: the
  target is given up and the shutter runs to the end, where the relay is left on.

### Software interlock on

- To reverse, the running relay goes off, and the other one goes on 0.5 s after the last
  off, whether we sent it or the relay reported it. If Home Assistant still shows the old
  relay on after our off, the reversal waits for it to show off for at most 2 s; after
  that it is given up and the shutter stays stopped.
- A relay switched on from outside while the other one is on (for example the wall button
  for the other direction while our order opens, or both wall buttons): the one that was
  on first is switched off, ours or not, and the shutter goes on in the new direction,
  with no pause (that relay is not ours to hold back). This is the only case in which a
  change from outside makes the integration switch a relay.
- If that new relay then goes off, the shutter stays stopped: the old relay is not
  switched on again. So a quick press and release of the other wall button is a stop,
  and so is a reading glitch of the idle relay (on and off again).
- After a restart with both relays on, the integration cannot tell which came on first:
  it keeps close and switches open off.

### Software interlock off

- The integration does not react to the relays. Whatever they do from outside, it
  switches none of them, ours included, until the next order from Home Assistant; only
  the position follows them. For example, while opening by our order, a wall button that
  switches the close relay on leaves both relays on: the open target is given up, the
  position follows close, and when close goes off again it follows open, which stays on
  up to the end and beyond (until the motor's limit switch stops it). The one exception
  is an off of ours that Home Assistant does not show yet: it still goes out again (see
  [Lost and failed commands](#lost-and-failed-commands)).
- With both relays on, the position follows the one switched on last (the newest wall
  press, or the relay of our latest order); when one goes off, the one that stays on. A
  relay module with its own interlock switches the old relay off by itself, and the
  shutter follows that.
- Orders from Home Assistant do not look at the other relay before turning one on, and
  leave a relay switched on from outside as it is. To reverse, the integration switches
  off the relay it turned on itself (if any) and switches the other on right after, with
  no pause and no checks. A stop switches off both relays. At the target of our order
  only our relay goes off: a relay of the other direction switched on from outside is
  left on, and the position follows it. For example, a wall button holds open and you ask
  for 30 %: close goes on, at 30 % close goes off, and the shutter opens again with the
  wall's relay up to the end, until open is switched off from outside or you stop it.

### Lost and failed commands

- Of each relay only the last command sent is kept. It is not sent again while Home
  Assistant does not show it, neither by a new order nor by a change of the other relay;
  if Home Assistant still does not show it 5 s after it was sent, it goes out again (if
  it is still wanted). A command lost on the way, a call that raises an error and a call
  that does not return within 5 s are all handled this way.
- An on is sent again only once. If Home Assistant still does not show the relay on 10 s
  after the order was given, the order is given up and the shutter stays stopped: a relay
  that answers again later does not start on its own. An off is not given up: it goes out
  every 5 s until Home Assistant shows it.
- Meanwhile the shutter shows what Home Assistant shows: an off that was lost means the
  motor keeps running, an on that was lost means it does not start.
- Within those 5 s, Home Assistant showing the other value of that relay is taken as an
  earlier command shown late, not as a wall button: the last order wins.
- A failed call makes the action that sent it fail (open, close, set position, stop).
  From the timer or after a change of a relay it is logged, only the first failure in a
  row as an error.

### Unavailable relays

The shutter is unavailable only when both relays are. With one relay unavailable it can
still be stopped, and the timer still switches off the other relay at its target. A move
that needs the missing relay is refused: it needs the relay of its direction, and with
interlock also the other one. An order
still waiting for a relay that becomes unavailable is given up 10 s after it was given, as
any order whose relay is not shown on by then; it never starts on its own when the relay
comes back later.

### Power cuts

Home Assistant saves the position every 15 minutes and when it stops. The integration
also has it saved when the shutter stops (not in the pause of a reversal), and once when
it reaches an end while a relay is still on, for example held by a wall switch.
After a power cut the position is the last one saved; the part of a movement that the cut
interrupts is lost. The position is also kept while the shutter is unavailable.

### Reloads and restarts

Reloading the helper (for example after changing the options or renaming a relay) or
restarting Home Assistant while the shutter moves loses the target: if the relay is still
on, the shutter takes it as switched on from outside, keeps going to its end and leaves
it on there. Removing or disabling the helper while it moves leaves the relay on until the
motor's limit switch stops it.

### Relays and the device registry

The switches are hidden when you create the helper or save its options with **Hide
relays** on; a switch you show again yourself stays shown after reloads and restarts.
Turning **Hide relays** off, choosing another relay, or deleting the helper shows again the
switches hidden by an integration, as the helpers of Home Assistant do: Home Assistant does
not record which integration hid a switch. Switches hidden by you stay hidden. Choosing
another relay (or swapping them) while a relay is on switches the old one off. If a relay
is renamed the helper follows it; if a relay is deleted a repair issue asks you to choose
another one in the options, and goes away when the helper is reloaded or Home Assistant
restarts with the relay added back.

## Recommended relay settings

- Set the relays to start **off** after a power cut (for example `PowerOnState 0` in
  Tasmota, or the default state "off" in Shelly). A relay that comes back on after a cut
  is taken as switched on from outside, and the integration does not switch it off.
- Use relays that report their real state to Home Assistant, so that wall buttons are
  followed.

## Limitations

- **Slow relays.** The integration assumes a relay carries out and reports a command
  within 5 s. Relays that take longer can make the shutter go back and forth on its own,
  and one that does not show an on within 10 s of the order makes the order be given up.
  With interlock, a relay that takes more than 2 s to show an off makes a reversal be
  given up: the shutter stops instead of going the other way.
- **Relays that report late.** The position and the timer count from when Home Assistant
  shows the relay on, and the motor is taken to run until it is shown off. Usual relays
  report within tenths of a second. One that reports seconds late runs that much too long
  on each order: the shutter goes past its target (from 0 to 50 % with 20 s of travel and
  a relay that reports 3 s late, it stops at 65 %), and the position counts that.
- **Relays that carry out queued commands late.** Home Assistant cannot tell whether such
  a relay has already carried out an on: both relays can be on for a moment during a quick
  reversal, even with the interlock on.
- **Relays whose state never changes in Home Assistant.** Integrations without feedback
  are optimistic: they show the command at once, and that works. A relay that neither
  reports nor shows our commands looks like one that loses them: the shutter sends an on
  once more after 5 s and gives the order up at 10 s, and its position does not move.
- **A relay switched on from outside while the other one runs** (for example a wall
  button for the other direction) is on together with the running one until the
  integration switches the running one off; the integration adds no pause. With the
  interlock off, the integration switches neither of them: both stay on until they are
  switched off from outside or the user stops the shutter. Use relays with their own
  interlock if your motor needs it.
- **Relays left on.** A relay switched on from outside stays on past the end until it is
  switched off from outside or the user stops the shutter; the motor's limit switch stops
  the motor. With the interlock off the same holds for a relay of ours after a wall
  button, and a reading glitch of the idle relay counts as a press too: it gives up the
  target and leaves our relay on.
- **A relay that shows our on too late.** If a relay switches on but Home Assistant does
  not show it within 10 s of the order (it dropped out, or it reports that late), the
  order is given up and no off is sent. If the relay is then shown on, that on is taken
  as a wall button: the relay is left on up to the end and beyond.
- **A command that may be a wall button.** Within 5 s of our last command to a relay,
  Home Assistant showing the other value is taken as that command not carried out yet,
  not as a wall button: the command goes out again 5 s after it was sent.
- **Wall buttons wired straight to the relays.** A button that switches a relay directly
  turns it on before the software can do anything: the interlock cannot stop both relays
  being on at once. Use a wiring or a relay module with a hardware interlock if your motor
  needs it.
- **Electronic tubular motors (for example Somfy with electronic limits).** Some reset to
  factory settings, losing their limits, when up and down are on together for about 7 s
  or receive certain sequences of short pulses. Keep the interlock on with these motors.

## License

MIT: see [LICENSE](LICENSE).

# ARIA Robot Body — Build Guide

A physical desktop body for ARIA: a pan/tilt head with a webcam (eyes),
a USB mic (ears), and a USB speaker (voice), driven by an Arduino Nano.
**No soldering required** — everything plugs into headers or screw terminals.

Phase 1 (this guide): head that looks around + face tracking. ~$55 in parts.
Phase 2 (wheels): two continuous-rotation servos turn it into a desk rover. ~$15 more.

## How it works

```
 ARIA (Python, your laptop)
   │  USB serial @115200, text protocol:  P<pan>T<tilt>   /  W<l>,<r>  /  S
   ▼
 Arduino Nano + sensor shield ──► head servos (D9 pan / D10 tilt)
                                  wheel servos (D5 / D6, phase 2)
   ▲
 USB webcam ──► eyes (vision.py, ARIA_BODY_CAMERA)
 USB mic ─────► ears (Windows default recording device)
 USB speaker ─► voice (Windows default playback device)
```

The Arduino side was the one missing piece of ARIA's existing `P90T45`
protocol — it now lives in `arduino/aria_body/aria_body.ino`. ARIA already had
`move_head_servos`, webcam capture, and face tracking; this guide gives those
bits a physical body. New tools: `drive_wheels` (phase 2) and `body_stop`.

## Parts — Phase 1 (~$55)

| Part | Why | Price found |
|---|---|---|
| Arduino Nano clone (CH340, with USB cable) | The muscle — generates servo signals, talks USB serial to ARIA | ~$8 (Inland Nano 3.0 is $7.99 at Micro Center, verified 2026-09-29) |
| Nano I/O expansion / sensor shield | Servos plug straight into its 3-pin headers — this is what makes it solder-free | ~$5 (Inland Nano shield $4.99 at Micro Center) |
| SG90 micro servos × 2 | Pan + tilt muscles | ~$4 each (Inland 3-pack $10.99 at Micro Center) |
| Mini pan-tilt bracket kit | The neck — two servos + webcam bolt together with screws | ~$6 (not carried at Micro Center — Amazon) |
| 1.3" SH1106 OLED display, I2C (128×64) | Her face — animated eyes + smile, driven by the Nano | ~$8 (Inland KS0056 $7.99 at Micro Center) |
| Female-to-female DuPont jumper wires | Connect the OLED (SDA/SCL/VCC/GND) to the shield's A4/A5 headers | maker aisle |
| 1080p USB webcam with built-in mic | Eyes + ears in one device | ~$16–20 (or use one you own) |
| Mini USB stereo speaker | Voice | ~$12–25 |
| 4×AA battery holder with on/off switch | Servo power — **separate from USB** (servos brown-out USB power when they move) | ~$3–6 |
| 4× AA batteries | | you probably own these |

Nothing else. The "body" itself is whatever you want: a small box, a 3D print,
a dinosaur toy with the head mounted on top. ARIA doesn't care what she looks like.

## Parts — Phase 2: mobile base (~$30, optional)

Verdict: **wheels, not tracks.** The cheapest decent tank-track kit runs $60+
and needs a motor-driver board plus different firmware. Two driven wheels do
everything a desk robot needs for ~$30, reuse the `drive_wheels` tool and
firmware already in this repo, and still involve zero soldering.

| Part | Why | Price found |
|---|---|---|
| FS90R continuous-rotation micro servos × 2 | The wheel motors — they *are* servos, so they plug into the same shield and speak the existing `W<l>,<r>` protocol. No motor driver needed. | ~$7.50 each |
| Servo wheels × 2 (for FS90R / micro servos) | Bolt straight onto the servo horns | ~$2.50 each |
| Ball caster × 1 | Rear third wheel so it doesn't drag | ~$2 |
| 2WD acrylic chassis plates | The clean sandwich decks — black acrylic, pre-cut | ~$4 |
| Brass standoffs M3 × 4 + screws | Hold the two decks apart; brass looks sharp against black acrylic | ~$2–7 |
| M2 screws/nuts (small kit) | Bolt the servo mounting ears to the bottom deck | ~$3 typical |

### How the clean build goes together

Think sandwich, not sprawl — every wire lives *between* the decks:

1. **Bottom deck (underside):** bolt the two FS90R servos to the plate with M2
   screws through their mounting ears, wheels on the horns. Ball caster at the
   rear. (Mark and drill 2 mm pilot holes if the plate's grid doesn't line up —
   sharp bit, low speed, acrylic drills fine.)
2. **Between decks:** Nano + sensor shield, 4×AA battery pack, and all wiring.
   Servo leads plug into D5 (left) / D6 (right). Nothing visible from outside.
3. **Top deck:** pan-tilt head dead center, mini USB speaker beside it.
4. **Standoffs** at the four corners set the deck gap (~30 mm clears the Nano).
5. **Cable bundle:** the three thin USB cables (Nano, webcam, speaker) zip-tied
   into one leash out the back to the laptop.

### The honest constraint: it's a desk rover

ARIA's brain runs on your laptop, so the robot is tethered by USB — it trundles
around the desk, turns to face you, spins, "paces" while thinking. True
untethered floor roaming would mean a Raspberry Pi + battery + WiFi redesign:
a different project at 3–4× the cost. This design doesn't pretend otherwise.

### Driving it

`drive_wheels` already exists: left/right −100…100, negative = reverse.
`drive_wheels(left=-60, right=60)` spins in place; add `seconds` and it
auto-stops (max 30 s) so it can't drive off the desk edge. `body_stop` kills
everything. Face tracking keeps working — the head tracks you while the base
sits still, or while it moves.

## Assembly — Phase 1 (no solder)

1. **Seat the Nano** in the sensor shield (pins line up one way; don't force it).
2. **Build the pan-tilt kit** per its instructions — bottom servo = pan, top = tilt.
   Mount the webcam on the top bracket with its tripod screw or a zip tie.
3. **Plug servos into the shield**: pan servo → D9 header, tilt servo → D10
   header. Servo plugs are keyed: brown/black wire = ground (−), red = 5V (+),
   orange/yellow = signal (S). Match the shield's labels.
4. **Servo power**: connect the 4×AA holder's leads to the shield's servo power
   terminal block (screw terminals — red to V+, black to GND). Flip the switch
   OFF for now. This powers the servos; the Nano itself is powered by USB.
5. **USB**: Nano → laptop. Webcam → laptop. Speaker → laptop. Mic is in the webcam.
6. **Windows sound settings**: set the USB speaker as default *playback* and the
   webcam mic as default *recording* device. ARIA uses system defaults — no code
   changes needed.

## The face (animated OLED eyes + smile)

A 1.3" SH1106 OLED (128×64, I2C) gives her a proper robot face: white eyes
with pupils that glance where the head turns, a blink every few seconds, and
a smile. It runs entirely on the Nano — no laptop code involved.

**Wiring (no solder).** The Inland KS0056 module must be set to **I2C mode**
(check the pads/jumper on the back of the module — bridge the I2C pads per
the silkscreen). Then with female-to-female DuPont jumpers:

| OLED pin | Shield header |
|---|---|
| VCC | 5V |
| GND | GND |
| SCL | A5 |
| SDA | A4 |

**Mounting.** Foam mounting tape on the front of the project box (or the head
assembly once the pan-tilt kit arrives). The face doesn't need to turn — the
pupils do the looking.

**Firmware.** `FACE_OLED` is `1` by default in `aria_body.ino`. Install the
**U8g2** library once (Arduino IDE: Sketch → Include Library → Manage
Libraries → search "U8g2"). Set `FACE_OLED` to `0` to build without the
display (saves ~10 KB flash). Verified to compile for the Nano: 44% flash,
46% RAM with the face enabled.

## Firmware (one-time, ~10 min)

1. Install Arduino IDE from arduino.cc.
2. Open `arduino/aria_body/aria_body.ino`.
3. Tools → Board → **Arduino Nano**. Tools → Processor → **ATmega328P
   (Old Bootloader)** (most CH340 clones need this).
4. Tools → Port → the COM port that appears when you plug the Nano in.
5. Upload. Open Serial Monitor @115200 — you should see `aria-body ready`.
6. Type `P0T45` → head pans full left. Type `P180T45` → full right.
   Type `S` → centers. If a servo spins the wrong way on phase 2 wheels,
   swap that servo's plug to the mirrored header or flip its sign in ARIA.

## ARIA software setup

1. `pip install -r requirements.txt` (pyserial was already a dependency).
2. Flip the battery switch ON, then start ARIA. `init_hardware()` auto-detects
   the Nano (looks for CH340 / USB Serial ports) and connects at 115200.
3. If the laptop's built-in camera should stay camera 0 and the body's webcam is
   the second device, set `ARIA_BODY_CAMERA=1` before launching ARIA.
4. Tell ARIA "look left" → `move_head_servos` fires → the head physically turns.
   "Follow my face" → `face_tracking` → the head tracks you via the webcam.

## Test procedure

### Testing with zero hardware: the virtual body

`sim/robot_sim.py` is a software stand-in for the Arduino — it speaks the exact
same `P/T/W/S` protocol over TCP, so ARIA can't tell the difference.

Terminal 1 (either laptop):

```
python sim/robot_sim.py --port 9999
```

Terminal 2 — point ARIA at it instead of a COM port:

```
# Windows
set ARIA_BODY_SERIAL_URL=socket://127.0.0.1:9999
# Linux/macOS
export ARIA_BODY_SERIAL_URL=socket://127.0.0.1:9999
```

Then start ARIA and use `move_head`, `drive_wheels`, `body_stop` normally.
Every command shows up live in terminal 1 with timestamps and wheel state,
e.g. `[21:14:23] DRIVE [L▲ +60 R▼ -60]`. Unset the variable to go back to
real USB serial. Across a LAN, replace `127.0.0.1` with the sim machine's IP —
handy if one laptop ends up being the robot's permanent host.

### The second-laptop play

Honestly the best use of a spare laptop: make it the robot's dedicated brain.
Install Python + `pip install -r requirements.txt`, pull this repo, set
`ARIA_BODY_CAMERA` if the webcam isn't index 0, and leave ARIA running there.
The robot tethers to it via USB; your main machine stays free, and you can
still reach ARIA from your phone through the phone bridge.

### With real hardware

- [ ] Serial Monitor: `P90T45` centers the head; servos move smoothly (eased).
- [ ] ARIA: "look left" moves the physical head.
- [ ] ARIA: "follow my face" tracks your face on the body webcam.
- [ ] Phone bridge: `/face.mjpg` now shows the body's point of view.
- [ ] Phase 2: "drive forward for 2 seconds" → `drive_wheels` → auto-stops.

### Phase 3: the phone face (optional)

An old Android phone (e.g. the SP555D) makes a better robot head than a bare
webcam: screen for a face/display, front camera for eyes, mic + speaker for
voice — all over WiFi, no extra USB cables to the moving head.

**Mechanical.** The phone (~150 g) is too heavy for the SG90 tilt servo, so
swap the *tilt* servo for a metal-gear MG90S (same size, same plug, same PWM —
drop-in, ~$10). Pan can stay an SG90. Clamp the phone in a universal tripod
mount and bolt the mount to the tilt platform (one drilled hole + 1/4"-20
bolt, or strong double-sided tape for a reversible fit). Keep the phone's
charger plugged in — the battery won't survive a day of streaming otherwise.

**Phone setup.** Factory reset, skip Google sign-in, connect to your WiFi,
install **IP Webcam** (free) and start its server — note the phone's IP.
Open ARIA's phone bridge page in the phone's Chrome for mic + speaker + the
on-screen interface.

**ARIA setup.** Point her eyes at the phone instead of the USB webcam:

```
# Windows
set ARIA_BODY_CAMERA=http://<phone-ip>:8080/video
# Linux/macOS
export ARIA_BODY_CAMERA=http://<phone-ip>:8080/video
```

`aria/vision/capture.py` (`body_camera_source()`) accepts a stream URL, a USB
index, or `bridge`, so face tracking and snapshots work unchanged.

### Bridge-page camera (one phone does everything)

Newer and simpler than IP Webcam: the bridge page itself can be ARIA's eyes.
Set `ARIA_BODY_CAMERA=bridge` on the laptop, open the bridge page on the
phone, and tap **Camera: ON** (in the ⚙ settings panel). The page streams
480x360 JPEG frames (~3 fps) to the laptop over the existing bridge
connection — no second app, no extra server. Face, voice, and eyes all live
in the one page. Tap **Camera: OFF** to stop. Uses the front camera (same
side as the screen, like real eyes); the **front ⇄** switch in the header
flips to the rear camera while streaming.

The redesigned bridge page (dark, minimal) has three view modes under the
stage: **Face** (her pixel body), **Eyes** (live view of her phone camera via
`/phone_cam.mjpg`), and **⛶ Full** (fullscreen face for the mounted phone —
tap anywhere to exit). In Eyes mode, **✦ Describe what you see** captures a
frame and has her describe it (`POST /api/look`). The 📜 button toggles the
scrollable conversation log; the directive field and hold/tap-to-talk
buttons sit below the stage.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Servos jitter / Nano resets when servos move | Servo power problem. Confirm the 4×AA pack is ON and wired to the shield's servo power terminals, not USB. Use fresh alkalines. |
| `Hardware: Virtual Mode` in ARIA logs | Nano not detected. Check USB cable (some cables are charge-only), correct COM port, CH340 driver installed (wch.cn). |
| Upload fails in Arduino IDE | Processor must be **ATmega328P (Old Bootloader)** for most clones. |
| Head moves opposite of command | Physically flip the servo horn 180°, or swap pan/tilt plugs. |
| Webcam shows laptop cam, not body cam | Set `ARIA_BODY_CAMERA=1` (or 2). |
| Wheels (phase 2) spin but robot turns in place | One wheel is mirrored — negate that side in the drive call, or flip the servo mount. |

## Safety

- `drive_wheels` with `seconds > 0` auto-stops (max 30 s) so ARIA can't drive off
  the desk if you walk away mid-command. `body_stop` kills everything.
- Keep the battery switch OFF when you're not using the body — SG90s hold
  position and drain AAs.
- Servos are strong enough to pinch fingers at the bracket joints. Don't stick
  fingers in the neck while it's tracking.

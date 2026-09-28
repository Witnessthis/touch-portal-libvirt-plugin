# touch-portal-libvirt-plugin

A Touch Portal plugin for `libvirt`: tracks whether USB devices are attached to a
VM, tracks each VM's power state, and can start/stop a VM and attach/detach its
USB devices — all read directly from `libvirt`, so it stays correct no matter what
attaches a device or starts a VM (a Touch Portal button, `virsh`, virt-manager,
...). Any number of VMs can be tracked at once, each with its own state and its
own entry in the VM Power / USB Device actions' VM dropdown.

## Requirements

- Touch Portal.
- `virsh`, with the VM defined on `qemu:///system` (not `qemu:///session` — check
  with `virsh uri`; the plugin always uses `qemu:///system` regardless).
- Python 3.
- A directory of hostdev XML descriptor files, one per device — the same files used
  with `virsh attach-device --file some-device.xml`:

  ```xml
  <hostdev mode="subsystem" type="usb" managed="yes">
    <source>
      <vendor id="0x1234"/>
      <product id="0x5678"/>
    </source>
  </hostdev>
  ```

  Vendor/product IDs come from `lsusb`. The file's name (`my-device.xml` →
  `my-device`) becomes the device's name in Touch Portal.

## Setup

1. Run `install.sh`. It symlinks this repo into Touch Portal's `plugins` folder —
   no `sudo`.
2. Start Touch Portal, or restart it if it was already running, and accept the
   "trust this plugin" prompt.
3. Under Settings → Plugins → libvirt Bridge, fill in:

   | Setting | Description |
   |---|---|
   | `VM Names` | Comma-separated libvirt domain names (`virsh --connect qemu:///system list --all`), e.g. `win11-physical, ubuntu-server`. |
   | `XML Directory` | Absolute path to the directory of hostdev XML files. |
   | `Attached Color` | Color for an attached device: `#RGB`, `#RRGGBB`, `#RGBA`, or `#RRGGBBAA` (alpha last). Default `#2ECC71`. |
   | `Detached Color` | Color for a detached device, same formats. Default `#555555`. |
   | `Other Color` | Color when a state is neither of those, same formats. Default `#F39C12`. |

   Saving scans immediately and colors every VM and device. Changes to any
   setting take effect without a restart. While `VM Names` or `XML Directory` is
   empty or invalid, the plugin does nothing: colors stop updating and VM Power
   presses are ignored (see `daemon.log`). No states are removed, so buttons work
   again as soon as the setting is fixed. Removing a VM from `VM Names` removes
   its color state (and drops it from the VM dropdowns) the same way a device
   removed from the XML directory does.

## Wiring a USB device button

A single button that both shows whether a device is attached and toggles it:

1. Button → **On Event** tab → add event → **When Plugin State changes**.
2. State: `<name> color` (the device's name, taken from its XML filename).
3. Condition: **does not change to**. Value: `0`.
4. Add action: **Change Button Visuals** → **Background Color (RAW)**.
5. In that field, press **+** and pick the same state (not a fixed color).
6. Button → **On Press** tab → add action → **USB Device**.
7. Device: pick the same device — the dropdown is populated from the XML
   directory and stays in sync as files are added or removed, no restart needed.
8. VM: pick which VM to attach/detach it on — the dropdown is populated from
   `VM Names` and stays in sync as that setting is edited.
9. Operation: `toggle` (also available: `attach`, `detach`).

A button doesn't need the state color wired up (steps 1-5) to use the action —
steps 6-9 alone are enough for an attach/detach/toggle button with no visual
feedback.

## Wiring a VM command button

A single button that both shows a VM's power state and sends it a command:

1. Button → **On Event** tab → add event → **When Plugin State changes**.
2. State: `<vm name> power color` (the VM's name, taken from `VM Names`).
3. Condition: **does not change to**. Value: `0`.
4. Add action: **Change Button Visuals** → **Background Color (RAW)**.
5. In that field, press **+** and pick the same state (not a fixed color).
6. Button → **On Press** tab → add action → **VM Power**.
7. VM: pick the same VM — the dropdown is populated from `VM Names` and stays
   in sync as that setting is edited, no restart needed.
8. Operation: `toggle` (also available: `start`, `shutdown`, `destroy`, `reboot`).

A button doesn't need the state color wired up (steps 1-5) to use the action —
steps 6-8 alone are enough for a plain command button with no visual feedback.

For a button where a short tap does one thing and a genuine long press does
something else entirely (e.g. tap to switch a display input, long-press to
toggle a VM), see the separate `touch-portal-holdtiming-plugin` project —
Touch Portal only allows a single action in a button's **On Hold** tab, so
gating this plugin's own action by hold duration doesn't work; a dedicated
timing plugin paired with plain actions like this one's does.

### Shutting down a Windows VM from the login screen

`shutdown` sends the same ACPI request as pressing the VM's power button. On the
one setup this was tested on, Windows ignored that request at the login screen
while its display was off — the first press only woke the display. So if the VM
is still running 5 seconds after a shutdown request, the plugin sends it once
more; you only press the button once. This may not match your VM at all: it can
depend on the Windows version, its power settings, and possibly the hardware or
drivers behind the passthrough devices, so treat the above as one possible
explanation rather than a guarantee of what you'll see.

> **Note:** if you do run into the display-only-wakes symptom and the second
> request still doesn't shut it down, you may need to do something like
> enabling Windows' hidden "forced button/lid shutdown" setting inside the VM
> (administrator PowerShell) — this is an example of the kind of fix that can
> help, not a verified solution for every setup:
>
> ```
> powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS SHUTDOWN 1
> powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS SHUTDOWN 1
> powercfg /setactive SCHEME_CURRENT
> ```
>
> This has only been tried on a handful of hardware/Windows version
> combinations, so whether it's needed — and whether this is the exact setting
> you need — may vary.
>
> **Warning:** this changes power-button behavior system-wide inside the VM.
> Once enabled, the power button closes programs without asking — unsaved work
> is lost if you shut down while signed in. Only run this if you understand
> what it does and are comfortable with that trade-off.

## License

MIT — see [LICENSE](LICENSE).

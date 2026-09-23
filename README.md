# Programming the Intel Edison's hidden microcontroller (MCU)

The Intel Edison has two brains:

- **The main CPU** (Intel Atom). It runs Linux and our Python code.
- **A small microcontroller (MCU)** inside the same chip. It's good for simple, fast, on-time
  jobs such as blinking pins, PWM or reading sensors, without Linux getting in the way.

Intel stopped supporting the Edison years ago, and modern Linux images can no longer talk to
the MCU. This folder brings it back. You write C on your Mac, compile it, send it to the Edison,
and talk to it from Linux.

---

## 1. The Edison image we use

| | |
|---|---|
| Image | Community build from **[edison-fw/meta-intel-edison](https://github.com/edison-fw/meta-intel-edison)** (`scarthgap` branch) |
| Linux | Poky (Yocto) 5.0.8, kernel `6.12.3-edison-acpi-preempt-rt` |
| Login | user `root`, no password |
| Console | USB serial, 115200 baud (on the Mac: `/dev/cu.usbserial-XXXX`) |

**What this image is missing for the MCU:**

- Intel's MCU drivers (`intel_psh_ipc`, `intel_mcu_common`). They only existed for the old
  3.10 kernel.
- The `/dev/ttymcu0`, `/dev/ttymcu1` devices.
- The boot service that loaded the MCU program. It is in the repo but switched off
  ("not supported by kernel").
- Kernel headers, so we can't build a new driver on the board.

**How we get around it:** the MCU hardware is still there and still works. We don't need a
driver. A Python script (`mcuctl.py`) does the driver's job straight from Linux.

---

## 2. How it works, in one picture

```
   Your Mac                         Intel Edison
 ┌───────────┐   serial/WiFi   ┌──────────────────────────────────────────┐
 │ main.c    │ ─────────────▶  │  Linux  ──mcuctl.py──▶  reserved RAM     │
 │   make    │  intel_mcu.bin  │                          (0x04819000)    │
 └───────────┘                 │                              │           │
                               │  MCU  ◀──── runs it ─────────┘           │
                               │   └── host_send() ──▶ mailbox ──▶ Linux  │
                               └──────────────────────────────────────────┘
```

- When the Edison boots, its firmware sets aside a piece of RAM for the MCU. Linux never uses it.
- `mcuctl.py` copies your program into that RAM and tells the MCU "run it".
- After that, the MCU and Linux send each other short messages through "mailboxes".

**Nothing gets flashed.** The program lives in RAM, so a reboot erases it.

---

## 3. One-time setup on the Mac

```bash
brew install i686-elf-gcc      # cross-compiler for the MCU (an old 32-bit x86 core)
pip3 install pyserial          # used by serie.py to talk over the USB serial port
```

That's all. The pieces of Intel's old SDK that we still need are already in `sdk/`.

---

## 4. Write and compile

Each program lives in its own folder with a `main.c`. The example is `hola/`:

```c
#include "mcu_api.h"

void mcu_main()                     // the MCU starts here (not main!)
{
    while (1) {
        host_send((unsigned char *)"hola\n", 5);   // send text to Linux
        mcu_sleep(100);                             // 100 ticks = 1 second
    }
}
```

To compile:

```bash
cd edison-mcu
make APP=hola                 # -> build/hola/intel_mcu.bin
```

For a new program, copy `hola/` to `myapp/`, edit `main.c`, and run `make APP=myapp`.

**Handy MCU functions** (full list in `sdk/mcu_api.h`):

| Function | What it does |
|---|---|
| `host_send(buf, len)` | send bytes to Linux (max 255, don't call it in a tight loop) |
| `host_receive(buf, len)` | read bytes that Linux sent you |
| `gpio_setup / gpio_read / gpio_write` | digital pins |
| `pwm_configure / pwm_enable` | PWM |
| `i2c_read / i2c_write` | I2C sensors |
| `uart_setup / uart_read / uart_write` | UART 1 or 2 |
| `mcu_sleep(ticks)` | sleep; 1 tick = 10 ms |
| `time_ms()`, `mcu_delay(us)` | time |
| `debug_print(level, fmt, ...)` | debug log |
| `mcu_snprintf(...)` | printf into a buffer (only `%d %x %s`) |

---

## 5. Send it to the Edison

**Over the USB serial cable** (works even without WiFi). Close CoolTerm or any other serial
app first, because only one program can use the port at a time:

```bash
python3 serie.py put build/hola/intel_mcu.bin /home/root/mcu/hola.bin
python3 serie.py put mcuctl.py /home/root/mcu/mcuctl.py      # only the first time
```

**Over WiFi** (faster), if the Edison is on your network:

```bash
scp build/hola/intel_mcu.bin root@edison.local:/home/root/mcu/hola.bin
```

`serie.py run 'command'` runs a command on the Edison and shows you its output.

---

## 6. Load it and talk to it (on the Edison)

```bash
cd /home/root/mcu
python3 mcuctl.py load hola.bin     # copy into the reserved RAM and start it
python3 mcuctl.py cat 10            # show what the MCU sends, for 10 seconds
python3 mcuctl.py send "ping"       # send text; your code reads it with host_receive()
python3 mcuctl.py probe             # just check the MCU is alive (changes nothing)
```

Expected output of `cat` for the `hola` example:

```
[mcu0] hola 0 t=38057
[mcu0] hola 1 t=39057
[mcu0] eco: ping
```

`diag_carga.py` is the "black box" version of `load`. It runs in the background, saves
everything to `/home/root/mcu/diag-N/`, and still works if the console goes bad:

```bash
setsid python3 diag_carga.py hola.bin > /dev/null 2>&1 < /dev/null &
```

---

## 7. Rules you must know

1. **One program per boot.** After the first load, the MCU refuses a new one ("Application
   Already Loaded"). To change it: replace the `.bin`, reboot the Edison, then load again.
   The original Intel Edison worked the same way.
2. **Loading messes up two serial ports.** While it starts your program, the MCU resets
   UART1 (`/dev/ttyS1`, the robot's lidar) and UART2 (`/dev/ttyS2`, the console), and Linux
   starts losing characters. `mcuctl.py load` fixes this by itself at the end. Bluetooth is not
   affected.
3. **Load the MCU first, then the rest.** On the robot, load the MCU program *before* starting
   anything that uses the lidar port.
4. The Edison has no clock battery. After a reboot the date is wrong, so to check whether it
   rebooted, use `cat /proc/uptime` (seconds since boot).

---

## 8. Files

| File | What it is |
|---|---|
| `Makefile` | compiles `APP/*.c` into `build/APP/intel_mcu.bin` |
| `hola/main.c` | example program: says hello every second and echoes what you send |
| `mcuctl.py` | runs **on the Edison**: loads the program and talks to the MCU |
| `diag_carga.py` | runs **on the Edison**: load + full log to files |
| `serie.py` | runs **on the Mac**: runs commands and uploads files over the USB serial cable |
| `sdk/intel_mcu.a`, `sdk/mcu.lds`, `sdk/mcu_api.h` | pieces of Intel's MCU SDK 1.0.10 |
| `sdk/libmini.c` | 3 small C functions the SDK expected (`memcpy`, `memset`, `strstr`) |
| `sdk/ejemplo-intel_mcu.bin` | Intel's original demo program, for comparison |

---

## 9. Where the pieces came from

- Old drivers (to learn the protocol): [htot/edison-linux](https://github.com/htot/edison-linux),
  `drivers/platform/x86/intel_psh_ipc.c` and `drivers/hwmon/intel_mcu_common.c`
- Intel demo `.bin` and the old boot service: [edison-fw/meta-intel-edison](https://github.com/edison-fw/meta-intel-edison),
  `meta-intel-edison-bsp/recipes-support/edison-mcu/`
- SDK library and linker script: [mrkcass/mechanizedAI](https://github.com/mrkcass/mechanizedAI),
  `mcu_motorcontroller/bin/`
- How the SDK built the `.bin`: [guermonprez/intel-academic-IoT-course](https://github.com/guermonprez/intel-academic-IoT-course),
  `labs/09_MicrocontrollerUnit/internal_tools/generate_mcu_bin.sh`
- API docs: [edison-fw/edison-wiki](https://edison-fw.github.io/edison-wiki/)

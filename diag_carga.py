#!/usr/bin/env python3
"""
diag_carga.py -- carga una app al MCU y registra TODO en archivos, sin depender de la consola.
Se lanza desacoplado (setsid) para que sobreviva si la consola serial se descompone:

  setsid python3 diag_carga.py hola.bin > /dev/null 2>&1 < /dev/null &

Deja en /home/root/mcu/diag-<n>/:
  pasos.log     que se hizo y cuando
  uart-*.txt    registros de los 3 UART y contadores de /proc/tty/driver/serial, antes y despues
  mcu.log       lo que el MCU mande por host_send (ttymcu0) y debug_print (ttymcu1)
Al final escribe un patron de prueba en la consola y, si hace falta, reengancha su driver.
"""
import os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcuctl

# 04.0 es el bloque comun del HSU; 04.1/04.2/04.3 = ttyS0 (BT) / ttyS1 (lidar) / ttyS2 (consola)
UARTS = {'ttyS0-bt': '0000:00:04.1', 'ttyS1-lidar': '0000:00:04.2', 'ttyS2-consola': '0000:00:04.3'}
# Solo registros cuya lectura no tiene efectos: nada de RBR (saca un byte), IIR (borra la
# interrupcion THRE), LSR/MSR (borran errores y deltas). Registros separados cada 4 bytes.
REGS = dict(IER=0x04, LCR=0x0c, MCR=0x10, SCR=0x1c, PS=0x30, MUL=0x34, DIV=0x38)
PATRON = 'PATRON-CONSOLA 0123456789 abcdefghijklmnopqrstuvwxyz FIN'

base = '/home/root/mcu'
n = 1
while os.path.exists('%s/diag-%d' % (base, n)):
    n += 1
out = '%s/diag-%d' % (base, n)
os.makedirs(out)
t0 = time.time()
flog = open(out + '/pasos.log', 'w', buffering=1)


def paso(msg):
    flog.write('%8.3f  %s\n' % (time.time() - t0, msg))
    os.fsync(flog.fileno())


def sh(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def foto(nombre):
    with open('%s/uart-%s.txt' % (out, nombre), 'w') as f:
        f.write('t=%.3f\n' % (time.time() - t0))
        for tag, dev in UARTS.items():
            try:
                b = mcuctl.Bar('/sys/bus/pci/devices/' + dev, 0)
                f.write('%-14s %s\n' % (tag, ' '.join('%s=%08x' % (k, b.r32(o)) for k, o in REGS.items())))
            except Exception as e:
                f.write('%-14s ERROR %s\n' % (tag, e))
        f.write(sh('cat /proc/tty/driver/serial') + '\n')
        f.write(sh("grep -E 'ttyS|hsu|dma|intel_psh' /proc/interrupts") + '\n')
        f.write(sh('stty -F /dev/ttyS2 -a 2>&1 | head -2') + '\n')
        f.write(sh('stty -F /dev/ttyS1 -a 2>&1 | head -1') + '\n')
        f.write(sh('hciconfig hci0 2>&1 | head -3') + '\n')
        f.flush()
        os.fsync(f.fileno())
    paso('foto %s' % nombre)


def consola(txt):
    """Escribe a la consola byte por byte (y de golpe) para ver que sobrevive desde la Mac."""
    try:
        fd = os.open('/dev/ttyS2', os.O_WRONLY | os.O_NOCTTY)
        os.write(fd, ('\r\n[golpe] %s\r\n' % txt).encode())
        time.sleep(0.3)
        for c in ('[lento] %s\r\n' % txt).encode():
            os.write(fd, bytes([c]))
            time.sleep(0.003)
        os.close(fd)
    except OSError as e:
        paso('consola: %s' % e)


fw = sys.argv[1] if len(sys.argv) > 1 else base + '/hola.bin'
paso('inicio, app=%s' % fw)
foto('0-antes')
consola(PATRON + ' (antes)')

p = mcuctl.Psh()
paso('PSH encendido: csr=%08x pimr1=%08x' % (p.ipc.r32(0x004), p.ipc.r32(0x500)))
p.poll()

# --- carga (igual que mcuctl.cmd_load, pero registrando cada paso) ---
data = open(fw, 'rb').read()
p.imr.write(0, data)
paso('copiado %d B al IMR @%08x' % (len(data), p.imr.phys))
try:
    p.command(3, mcuctl.CMD_LOAD_APP, p.imr.phys, timeout=3)
    paso('LOAD_APP aceptado (ia2psh[3] bajo BUSY)')
except Exception as e:
    paso('LOAD_APP: %r' % e)
foto('1-tras-load')
try:
    paso('psh2ia[2] tras LOAD: msg=%08x param=%08x' % p.wait_ch(2, 5))
except Exception as e:
    paso('psh2ia[2] tras LOAD: %r' % e)
try:
    p.command(2, mcuctl.CMD_SETUP_DDR, p.ddr.phys, timeout=3)
    paso('SETUP_DDR aceptado')
    paso('psh2ia[2] tras SETUP_DDR: msg=%08x param=%08x' % p.wait_ch(2, 5))
except Exception as e:
    paso('SETUP_DDR: %r' % e)
for fr in p.lbuf_frames(2):
    paso('cmd_resp %r' % (mcuctl.resp_payload(fr),))
foto('2-tras-ddr')

# --- escuchar al MCU 20 s ---
with open(out + '/mcu.log', 'w', buffering=1) as f:
    fin = time.time() + 20
    while time.time() < fin:
        for ch, m, prm in p.poll():
            f.write('%8.3f ipc ch%d msg=%08x param=%08x\n' % (time.time() - t0, ch, m, prm))
            if ch in p.lbuf:
                for fr in p.lbuf_frames(ch):
                    cid, ret, d = mcuctl.resp_payload(fr)
                    f.write('%8.3f  ch%d %r\n' % (time.time() - t0, ch, d if ch != 2 else (cid, ret, d)))
        time.sleep(0.002)
    os.fsync(f.fileno())
paso('fin de escucha (mcu.log)')

try:
    p.command(0, 0, int.from_bytes(b'ping', 'little'), timeout=2)
    paso('mandado "ping" a host_receive')
    time.sleep(1)
    for ch, m, prm in p.poll():
        for fr in p.lbuf_frames(ch) if ch in p.lbuf else []:
            paso('respuesta ch%d %r' % (ch, mcuctl.resp_payload(fr)[2]))
except Exception as e:
    paso('ping: %r' % e)

foto('3-tras-escucha')
consola(PATRON + ' (despues)')

# --- reparacion ---
modo = sys.argv[2] if len(sys.argv) > 2 else 'stty'
if modo == 'rebind':
    drv = '/sys/bus/pci/drivers/8250_mid'
    paso('unbind/bind %s' % UARTS['ttyS2-consola'])
    sh('echo %s > %s/unbind' % (UARTS['ttyS2-consola'], drv))
    time.sleep(1)
    sh('echo %s > %s/bind' % (UARTS['ttyS2-consola'], drv))
    time.sleep(2)
    sh('systemctl restart serial-getty@ttyS2')
else:
    # cambiar la velocidad ida y vuelta obliga a 8250_mid a reescribir MUL/DIV, divisor y FCR
    for tty in ('ttyS2', 'ttyS1'):
        paso('stty %s: %s' % (tty, sh('v=$(stty -F /dev/%s speed); stty -F /dev/%s 9600; '
                                      'stty -F /dev/%s $v; stty -F /dev/%s speed' % ((tty,) * 4))))
time.sleep(1)
foto('4-tras-%s' % modo)
consola(PATRON + ' (tras %s)' % modo)
paso('listo')
sh('sync')

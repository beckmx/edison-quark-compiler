#!/usr/bin/env python3
"""
mcuctl.py -- hablarle al MCU (PSH) del Edison desde espacio de usuario, sin driver.

Reimplementa lo que hacian intel_psh_ipc.c + intel_mcu_common.c del BSP 3.10
(htot/edison-linux, drivers/platform/x86 y drivers/hwmon), pero con sysfs:
  - /sys/bus/pci/devices/0000:00:16.0   PSH IPC: buzones ia2psh / psh2ia
  - /sys/bus/pci/devices/0000:00:16.1   BAR0 = IMR de la app (512K), BAR1 = buffers (32K)
En vez de IRQ se hace polling de PISR y del bit BUSY de cada buzon.

Uso (en el Edison, como root):
  mcuctl.py probe            enciende los dispositivos y vuelca registros (no escribe al PSH)
  mcuctl.py load app.bin     copia la app al IMR, LOAD_APP + SETUP_DDR, repara ttyS1/ttyS2
                             (el PSH acepta UNA app por arranque: "Application Already Loaded")
  mcuctl.py cat [seg]        imprime lo que mande el MCU (ttymcu0 = host_send, ttymcu1 = debug)
  mcuctl.py send "texto"     manda texto a host_receive()
  mcuctl.py version          CMD_MCU_APP_GET_VERSION
"""
import ctypes, mmap, os, struct, sys, time

IPC = '/sys/bus/pci/devices/0000:00:16.0'
MCU = '/sys/bus/pci/devices/0000:00:16.1'

BUSY = 1 << 31
CONTINUE = 1 << 30

# Mapa de registros "B step" (Tangier), offsets en bytes dentro del BAR0 de 00:16.0
R = dict(pimr0=0x000, csr=0x004, pmctl=0x008, pmstat=0x00c, msi=0x010,
         pimr3=0x100, scu2psh=0x104, psh2scu=0x10c,
         pisr=0x400, scratch0=0x404, scratch1=0x408,
         pimr1=0x500, pimr2=0x800)
def IA2PSH(ch): return 0x504 + 8 * ch     # msg, param
def PSH2IA(ch): return 0x524 + 8 * ch
def ST_PSH2IA(ch): return 1 << (ch + 6)

CMD_LOAD_APP, CMD_SETUP_DDR, CMD_APP_DEBUG, CMD_GET_VERSION = range(4)
LBUF_CELL, LBUF_EMPTY, LBUF_DISCARD = 0x4853, 0x0000, 0x4944
BUF_SZ = 8192
APP_IMR_SIZE = 126 * 1024


_libc = ctypes.CDLL(None, use_errno=True)
_mmap = _libc.mmap
_mmap.restype = ctypes.c_void_p
_mmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int,
                  ctypes.c_int, ctypes.c_long)


def sysfs_write(path, val):
    with open(path, 'w') as f:
        f.write(val)


def sysfs_read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        return '?(%s)' % e.strerror


def resource(dev, bar):
    """(fisica, tamano) del BAR segun el archivo 'resource' de sysfs."""
    with open(dev + '/resource') as f:
        start, end, _ = (int(x, 16) for x in f.readlines()[bar].split())
    return start, end - start + 1


class Bar:
    def __init__(self, dev, bar):
        self.phys, self.size = resource(dev, bar)
        self.fd = os.open('%s/resource%d' % (dev, bar), os.O_RDWR | os.O_SYNC)
        # sysfs deja mapear hasta el tamano redondeado a pagina (el BAR del IPC
        # mide 256 B pero los registros llegan a 0x810, igual que en 3.10)
        # mmap de libc: el de Python se niega a pasar del tamano que reporta sysfs
        # sysfs mapea desde el inicio de la pagina: los UART viven en 0xFF010x80
        pgoff = self.phys & (mmap.PAGESIZE - 1)
        self.maplen = (pgoff + self.size + mmap.PAGESIZE - 1) & ~(mmap.PAGESIZE - 1)
        addr = _mmap(None, self.maplen, mmap.PROT_READ | mmap.PROT_WRITE,
                     mmap.MAP_SHARED, self.fd, 0)
        if addr in (None, ctypes.c_void_p(-1).value):
            raise OSError(ctypes.get_errno(), 'mmap %s/resource%d' % (dev, bar))
        addr += pgoff
        self.addr = addr
        self.mm = (ctypes.c_char * (self.maplen - pgoff)).from_address(addr)

    # accesos de 32 bits de verdad (un slice puede partirlos en bytes)
    def r32(self, off):
        return ctypes.c_uint32.from_address(self.addr + off).value

    def w32(self, off, v):
        ctypes.c_uint32.from_address(self.addr + off).value = v & 0xffffffff

    def r16(self, off):
        return ctypes.c_uint16.from_address(self.addr + off).value

    def write(self, off, data):
        ctypes.memmove(self.addr + off, data, len(data))

    def read(self, off, n):
        return ctypes.string_at(self.addr + off, n)


def power_on():
    for dev in (IPC, MCU):
        if sysfs_read(dev + '/enable') != '1':
            sysfs_write(dev + '/enable', '1')
        try:
            sysfs_write(dev + '/power/control', 'on')
        except OSError:
            pass


def show_power():
    for dev in (IPC, MCU):
        print('%s  power_state=%s enable=%s runtime=%s' % (
            dev[-7:], sysfs_read(dev + '/power_state'),
            sysfs_read(dev + '/enable'), sysfs_read(dev + '/power/runtime_status')))


class Psh:
    def __init__(self):
        power_on()
        self.ipc = Bar(IPC, 0)
        self.imr = Bar(MCU, 0)
        self.ddr = Bar(MCU, 1)
        # igual que mcu_platform_probe: i = 2,1,0 -> base, base+8K, base+16K
        self.lbuf = {2: [0, 0], 1: [BUF_SZ, 0], 0: [2 * BUF_SZ, 0]}   # ch: [base, off_head]

    def dump(self):
        for k, off in R.items():
            print('  %-9s @%03x = %08x' % (k, off, self.ipc.r32(off)))
        for ch in range(4):
            print('  ia2psh[%d] msg=%08x param=%08x   psh2ia[%d] msg=%08x param=%08x' % (
                ch, self.ipc.r32(IA2PSH(ch)), self.ipc.r32(IA2PSH(ch) + 4),
                ch, self.ipc.r32(PSH2IA(ch)), self.ipc.r32(PSH2IA(ch) + 4)))

    def command(self, ch, msg, param, timeout=1.0):
        """intel_ia2psh_command(): param, luego msg|BUSY; espera a que el PSH baje BUSY."""
        o = IA2PSH(ch)
        if self.ipc.r32(o) & BUSY:
            raise RuntimeError('ia2psh[%d] ocupado (%08x)' % (ch, self.ipc.r32(o)))
        self.ipc.w32(o + 4, param)
        self.ipc.w32(o, msg | BUSY)
        if timeout == 0:
            return None
        t = time.time() + timeout
        while self.ipc.r32(o) & BUSY:
            if time.time() > t:
                raise TimeoutError('ia2psh[%d] sigue ocupado: el PSH no respondio' % ch)
            time.sleep(0.0001)
        return self.ipc.r32(o), self.ipc.r32(o + 4)

    def poll(self):
        """Revisa los 4 buzones psh2ia; devuelve [(ch, msg, param)] y los libera."""
        out = []
        pisr = self.ipc.r32(R['pisr'])
        for ch in range(4):
            m = self.ipc.r32(PSH2IA(ch))
            if (pisr & ST_PSH2IA(ch)) or (m & BUSY):
                p = self.ipc.r32(PSH2IA(ch) + 4)
                self.ipc.w32(PSH2IA(ch), m & ~BUSY)     # escribir de vuelta limpia BUSY
                out.append((ch, m & ~BUSY, p))
        return out

    def lbuf_frames(self, ch):
        """lbuf_read_next(): recorre los marcos 'SH' del buffer circular del canal."""
        base, head = self.lbuf[ch]
        frames = []
        for _ in range(512):
            sign = self.ddr.r16(base + head)
            if sign == LBUF_DISCARD:
                head = 0
                sign = self.ddr.r16(base)
            if sign != LBUF_CELL:
                break
            ln = self.ddr.r16(base + head + 2)
            frames.append(self.ddr.read(base + head + 4, ln))
            head += ((ln + 3) & ~3) + 4
            if head >= BUF_SZ:
                head = 0
        self.lbuf[ch][1] = head
        return frames

    def wait_ch(self, ch, timeout=3.0):
        t = time.time() + timeout
        while time.time() < t:
            for c, m, p in self.poll():
                if c == ch:
                    return m, p
            time.sleep(0.001)
        raise TimeoutError('sin respuesta en psh2ia[%d]' % ch)


def resp_payload(fr):
    """struct cmd_resp { u8 cmd_id; u8 len; int ret; char param[56]; } __packed"""
    if len(fr) < 6:
        return None, 0, b''
    cid, ln, ret = struct.unpack('<BBi', fr[:6])
    return cid, ret, fr[6:6 + ln]


def cmd_probe():
    show_power()
    print('encendiendo 00:16.0 y 00:16.1 ...')
    p = Psh()
    show_power()
    print('IPC  BAR0 %08x (%d B, mapeado %d)' % (p.ipc.phys, p.ipc.size, p.ipc.maplen))
    print('IMR  BAR0 %08x (%d B)   DDR BAR1 %08x (%d B)' % (p.imr.phys, p.imr.size, p.ddr.phys, p.ddr.size))
    p.dump()
    head = p.imr.read(0, 16).hex()
    print('  IMR[0:16] = %s' % head)
    alive = p.ipc.r32(R['csr']) != 0xffffffff
    print('\n=> registros %s' % ('legibles' if alive else 'en 0xffffffff: el bloque sigue apagado'))


def reparar_uarts():
    """Tras LOAD_APP el cargador del PSH reinicializa UART1 (ttyS1) y UART2 (ttyS2, la
    consola): les cambia MUL/DIV y la FIFO, y las rafagas de Linux se pierden. Cambiar la
    velocidad ida y vuelta obliga a 8250_mid a reprogramarlos sin cerrar el puerto."""
    import subprocess
    for tty in ('ttyS2', 'ttyS1'):
        subprocess.run('v=$(stty -F /dev/%s speed) && stty -F /dev/%s 9600 && stty -F /dev/%s $v'
                       % (tty, tty, tty), shell=True, capture_output=True)


def cmd_load(path):
    fw = open(path, 'rb').read()
    words = struct.unpack('<I', fw[724:728])[0]
    if words * 4 != len(fw) - 728:
        sys.exit('%s: cabecera invalida (%d palabras vs %d bytes)' % (path, words, len(fw) - 728))
    if len(fw) > APP_IMR_SIZE:
        sys.exit('app demasiado grande: %d > %d' % (len(fw), APP_IMR_SIZE))
    p = Psh()
    p.poll()                                     # limpiar lo que hubiera pendiente
    p.imr.write(0, fw)
    print('copiado %d B al IMR @%08x; LOAD_APP por ia2psh[3] ...' % (len(fw), p.imr.phys))
    p.command(3, CMD_LOAD_APP, p.imr.phys)
    print('  aceptado; esperando confirmacion en psh2ia[2] ...')
    print('  psh2ia[2] -> msg=%08x param=%08x' % p.wait_ch(2))
    print('SETUP_DDR @%08x por ia2psh[2] ...' % p.ddr.phys)
    p.command(2, CMD_SETUP_DDR, p.ddr.phys)
    try:
        print('  psh2ia[2] -> msg=%08x param=%08x' % p.wait_ch(2))
    except TimeoutError as e:
        print('  (%s)' % e)
    for fr in p.lbuf_frames(2):
        print('  cmd_resp:', resp_payload(fr))
    reparar_uarts()
    print('listo.')


def cmd_cat(secs):
    p = Psh()
    t = time.time() + secs
    names = {0: 'mcu0', 1: 'dbg ', 2: 'cmd '}
    while time.time() < t:
        for ch, m, _ in p.poll():
            if ch in p.lbuf:
                for fr in p.lbuf_frames(ch):
                    cid, ret, data = resp_payload(fr)
                    txt = data.decode('utf-8', 'replace') if ch != 2 else repr((cid, ret, data))
                    sys.stdout.write('[%s] %s' % (names[ch], txt if txt.endswith('\n') else txt + '\n'))
                    sys.stdout.flush()
        time.sleep(0.002)


def cmd_send(text):
    """raw_output(): 4 bytes por mensaje en param; CONTINUE en todos menos el ultimo."""
    p = Psh()
    b = text.encode()
    for i in range(0, len(b), 4):
        chunk = b[i:i + 4]
        last = i + 4 >= len(b)
        p.command(0, 0 if last else CONTINUE, struct.unpack('<I', chunk.ljust(4, b'\0'))[0])
    print('enviados %d B' % len(b))


def cmd_version():
    p = Psh()
    p.poll()
    p.command(2, CMD_GET_VERSION, 0)
    p.wait_ch(2)
    for fr in p.lbuf_frames(2):
        cid, ret, data = resp_payload(fr)
        if cid == CMD_GET_VERSION and len(data) >= 3:
            print(data[3:3 + data[0]].split(b'\0')[0].decode('utf-8', 'replace'))
        else:
            print('resp', cid, ret, data)


if __name__ == '__main__':
    a = sys.argv[1:] or ['probe']
    {'probe': lambda: cmd_probe(),
     'load': lambda: cmd_load(a[1]),
     'cat': lambda: cmd_cat(float(a[1]) if len(a) > 1 else 10),
     'send': lambda: cmd_send(a[1]),
     'version': lambda: cmd_version()}[a[0]]()

#!/usr/bin/env python3
"""
serie.py -- consola del Edison por el FTDI, para cuando no hay red.
  serie.py run 'comando' ['otro' ...]     corre y muestra la salida
  serie.py put local /ruta/remota         sube un archivo (base64 en lineas cortas)
Cierra CoolTerm antes: macOS no deja abrir el puerto dos veces.
"""
import base64, hashlib, os, re, serial, sys, time

PORT = os.environ.get('EDISON_TTY', '/dev/cu.usbserial-DA01LPFK')
MARK = '__FIN_%d__'


class Consola:
    def __init__(self):
        self.s = serial.Serial(PORT, 115200, timeout=0.2)
        self.n = 0
        self.s.write(b'\n')
        self._leer(1.0)
        self.s.write(b'stty -echo 2>/dev/null\n')
        self._leer(1.0)

    def _leer(self, t):
        out, end = b'', time.time() + t
        while time.time() < end:
            d = self.s.read(4096)
            if d:
                out += d
        return out

    def run(self, cmd, timeout=60):
        """Ejecuta y regresa la salida hasta el marcador (sin eco ni prompt)."""
        self.n += 1
        mark = MARK % self.n
        self.s.write(('%s; echo %s$?\n' % (cmd, mark)).encode())
        buf, end = b'', time.time() + timeout
        pat = re.compile((mark + r'(\d+)').encode())
        while time.time() < end:
            buf += self.s.read(4096)
            m = pat.search(buf)
            if m:
                txt = buf[:m.start()].decode('utf-8', 'replace')
                txt = re.sub(r'\x1b\[[0-9;]*m', '', txt).replace('\r', '')
                return txt, int(m.group(1))
        raise TimeoutError('%r no termino en %ds; salida parcial:\n%s' % (cmd, timeout, buf.decode('utf-8', 'replace')))

    def put(self, local, remote):
        data = open(local, 'rb').read()
        b64 = base64.b64encode(data).decode()
        tmp = remote + '.b64'
        self.run(': > %s' % tmp)
        step = 700
        for i in range(0, len(b64), step * 8):
            lines = [b64[j:j + step] for j in range(i, min(i + step * 8, len(b64)), step)]
            self.run(' && '.join("echo '%s' >> %s" % (l, tmp) for l in lines))
        out, rc = self.run("python3 -c \"import base64,hashlib,sys; d=base64.b64decode(open('%s').read()); "
                           "open('%s','wb').write(d); print(hashlib.md5(d).hexdigest())\" && command rm -f %s" % (tmp, remote, tmp))
        want = hashlib.md5(data).hexdigest()
        ok = want in out
        print('%s -> %s  %d B  md5 %s' % (local, remote, len(data), 'OK' if ok else 'DISTINTO: ' + out))
        return ok


if __name__ == '__main__':
    c = Consola()
    if sys.argv[1] == 'run':
        rc = 0
        for cmd in sys.argv[2:]:
            out, rc = c.run(cmd, timeout=float(os.environ.get('T', 60)))
            sys.stdout.write(out)
        sys.exit(rc)
    elif sys.argv[1] == 'put':
        sys.exit(0 if c.put(sys.argv[2], sys.argv[3]) else 1)

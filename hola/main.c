/*
 * Primera app para el MCU del Edison:
 *  - cada segundo manda "hola <n> t=<ms>" por /dev/ttymcu0
 *  - lo que llegue por /dev/ttymcu0 lo regresa con prefijo "eco: "
 */
#include "mcu_api.h"
#include "mcu_errno.h"

void mcu_main()
{
	char buf[64];
	unsigned char rx[64];
	int n, len, i = 0;
	unsigned long t0 = time_ms();

	debug_print(DBG_INFO, "hola: arranque\n");
	while (1) {
		len = host_receive(rx, sizeof(rx) - 1);
		if (len > 0) {
			rx[len] = 0;
			n = mcu_snprintf(buf, sizeof(buf), "eco: %s\n", (char *)rx);
			host_send((unsigned char *)buf, n);
		}
		if (time_ms() - t0 >= 1000) {
			t0 += 1000;
			n = mcu_snprintf(buf, sizeof(buf), "hola %d t=%d\n", i++, (int)time_ms());
			host_send((unsigned char *)buf, n);
		}
		mcu_sleep(1);	/* 1 tick = 10 ms */
	}
}

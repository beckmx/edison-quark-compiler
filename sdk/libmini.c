/* Lo unico que intel_mcu.a le pedia a la newlib del SDK original. */
#include <stddef.h>

void *memcpy(void *d, const void *s, size_t n)
{
	unsigned char *a = d;
	const unsigned char *b = s;
	while (n--)
		*a++ = *b++;
	return d;
}

void *memset(void *d, int c, size_t n)
{
	unsigned char *a = d;
	while (n--)
		*a++ = (unsigned char)c;
	return d;
}

char *strstr(const char *h, const char *n)
{
	size_t i;
	if (!*n)
		return (char *)h;
	for (; *h; h++) {
		for (i = 0; n[i] && h[i] == n[i]; i++)
			;
		if (!n[i])
			return (char *)h;
	}
	return NULL;
}

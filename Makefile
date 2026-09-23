# App para el MCU (Minute IA / Quark) del Edison, sin el SDK de Eclipse.
#   make APP=hola      -> build/hola/intel_mcu.bin
#   make APP=hola install EDISON=root@edison.local
# Receta tomada de internal_tools/generate_mcu_bin.sh del MCU SDK 1.0.10.

APP     ?= hola
EDISON  ?= root@edison.local
CROSS   ?= i686-elf-
CC       = $(CROSS)gcc
LD       = $(CROSS)ld
OBJCOPY  = $(CROSS)objcopy
OBJDUMP  = $(CROSS)objdump

SDK = sdk
OUT = build/$(APP)

# El MCU es un nucleo tipo i486/Pentium sin FPU util ni SSE; el SDK usaba gcc 4.6.
CFLAGS  = -m32 -march=i486 -mno-sse -mno-mmx -msoft-float -O2 -Wall \
          -ffreestanding -fno-pic -fno-stack-protector -fno-builtin \
          -fno-asynchronous-unwind-tables -fno-unwind-tables \
          -ffunction-sections -fdata-sections -I$(SDK)
LDFLAGS = -X -N --gc-sections -Ttext 0xFF300000 -e __Start -static \
          --no-undefined -nostdlib -m elf_i386

SRCS = $(wildcard $(APP)/*.c) $(SDK)/libmini.c
OBJS = $(patsubst %.c,$(OUT)/%.o,$(notdir $(SRCS)))

all: $(OUT)/intel_mcu.bin

$(OUT)/%.o: $(APP)/%.c | $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@
$(OUT)/%.o: $(SDK)/%.c | $(OUT)
	$(CC) $(CFLAGS) -c $< -o $@
$(OUT):
	mkdir -p $@

$(OUT)/intel_mcu.elf: $(OBJS) $(SDK)/intel_mcu.a $(SDK)/mcu.lds
	$(LD) -T $(SDK)/mcu.lds $(LDFLAGS) $(SDK)/intel_mcu.a $(OBJS) -o $@
	$(OBJDUMP) -D $@ > $(OUT)/intel_mcu.dump

# 728 bytes de cabecera en cero; en el offset 724 el tamano del raw en palabras.
$(OUT)/intel_mcu.bin: $(OUT)/intel_mcu.elf
	$(OBJCOPY) -j .pshinit -j .builtin_fw -O binary -j .text -j .rodata \
	  -j .data -j .bss --set-section-flags .bss=alloc,load,contents $< $(OUT)/intel_mcu.raw
	python3 -c "import struct,sys; r=open(sys.argv[1],'rb').read(); \
	  h=bytearray(728); h[724:728]=struct.pack('<I',len(r)//4); \
	  open(sys.argv[2],'wb').write(bytes(h)+r)" $(OUT)/intel_mcu.raw $@
	@ls -l $@

install: $(OUT)/intel_mcu.bin
	scp $< $(EDISON):/lib/firmware/intel_mcu.bin

clean:
	rm -rf build

.PHONY: all install clean

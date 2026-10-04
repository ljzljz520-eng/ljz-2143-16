CC := gcc
CFLAGS := std=c11

SDL_CFLAGS = $(shell sdl2-config --cflags 2>/dev/null)
SDL_LIBS = $(shell sdl2-config --libs 2>/dev/null)
LDLIBS := $(SDL_LIBS) -lSDL2_image

SRC_DIR := src
TARGET := visual-window-app
SOURCES := $(SRC_DIR)/main.c $(SRC_DIR)/window.c $(SRC_DIR)/renderer.c

# ---- 共建事务：设备端 + Web 控制台（无 SDL 依赖，headless 可运行）----
PYTHON ?= python3
DEVICE_SRC := device/device.c
DEVICE_BIN := device/device
DEVICE_CFLAGS := -std=c11 -O2 -Wall -Wextra -Werror -pthread

.PHONY: all clean run device console demo demo-clean device-clean

all: $(TARGET)

$(TARGET): $(SOURCES)
	$(CC) -$(CFLAGS) -O2 -Wall -Wextra -Werror $(SDL_CFLAGS) $(SOURCES) -o $@ $(LDLIBS)

run: $(TARGET)
	./$(TARGET)

clean:
	rm -f $(TARGET)

device: $(DEVICE_BIN)

$(DEVICE_BIN): $(DEVICE_SRC)
	gcc $(DEVICE_CFLAGS) $(DEVICE_SRC) -o $(DEVICE_BIN)

console: $(DEVICE_BIN)
	$(PYTHON) server/app.py

demo-clean:
	rm -f runtime/console.db runtime/console.db-wal runtime/console.db-shm \
	      runtime/device.stderr.log

device-clean:
	rm -f $(DEVICE_BIN)

demo: $(DEVICE_BIN) demo-clean
	@echo "启动服务 -> 运行 4 个事务场景演示 -> 停止服务"
	@PORT=8080 $(PYTHON) server/app.py > runtime/server.log 2>&1 & echo $$! > runtime/server.pid
	@for i in $$(seq 1 40); do \
	  $(PYTHON) -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/api/state',timeout=1)" 2>/dev/null && break; \
	  sleep 0.25; \
	done
	@$(PYTHON) demo/run_demo.py; rc=$$?; \
	pid=$$(cat runtime/server.pid); kill $$pid 2>/dev/null || true; sleep 0.6; \
	pkill -P $$pid 2>/dev/null || true; \
	rm -f runtime/server.log runtime/server.pid; \
	exit $$rc

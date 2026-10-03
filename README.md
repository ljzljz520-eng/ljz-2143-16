# Visual Window App (C + SDL2 + noVNC)

这是一个真实的 **C 语言桌面 GUI 程序**（基于 SDL2），程序在 Linux 容器中启动窗口并将图片渲染为背景图。
通过 **Xvfb + x11vnc + noVNC**，你可以直接在浏览器看到并操作这个窗口。

## 特性

- C11 + SDL2 图形窗口（非 Web 页面）
- SDL2_image 加载背景图（`assets/background.png`）
- 清晰事件循环（刷新、关闭事件）
- 严格编译参数：`-Wall -Wextra -Werror`
- 一条命令启动整套可视化链路

## 技术架构

```text
Browser (http://localhost:6080)
        |
      noVNC (WebSocket)
        |
      x11vnc (VNC)
        |
      Xvfb (:99 virtual display) + fluxbox
        |
   C SDL2 GUI App (real window rendering)
```

## 目录结构

```text
visual-window-app/
├── docker-compose.yml
├── README.md
├── assets/
│   └── background.png
├── docker/
│   ├── Dockerfile
│   └── entrypoint.sh
├── src/
│   ├── main.c
│   ├── window.c
│   ├── window.h
│   ├── renderer.c
│   └── renderer.h
└── Makefile
```

## 一键启动

```bash
docker compose up --build
```

启动完成后，在浏览器打开：

```text
http://localhost:6080
```

## noVNC 说明

noVNC 仅用于远程显示容器中的真实图形窗口。
本项目本质仍是标准 C GUI 程序，不是 Web 应用，也不是 Canvas 模拟。

## 背景图

当前仓库默认提供了 `assets/background.png` 作为占位背景图。
如果你要替换成指定图片，直接覆盖同路径文件即可，无需改代码。


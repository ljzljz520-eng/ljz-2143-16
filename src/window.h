#ifndef WINDOW_H
#define WINDOW_H

#include <stdbool.h>
#include <SDL2/SDL.h>

typedef struct {
    SDL_Window *window;
    SDL_Renderer *renderer;
    int width;
    int height;
} AppWindow;

bool window_init(AppWindow *app, const char *title, int width, int height);
void window_destroy(AppWindow *app);

#endif

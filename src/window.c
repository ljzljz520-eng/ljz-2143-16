#include "window.h"

#include <stdio.h>

bool window_init(AppWindow *app, const char *title, int width, int height) {
    if (app == NULL) {
        fprintf(stderr, "window_init: app is NULL\n");
        return false;
    }

    app->window = NULL;
    app->renderer = NULL;
    app->width = width;
    app->height = height;

    app->window = SDL_CreateWindow(
        title,
        SDL_WINDOWPOS_CENTERED,
        SDL_WINDOWPOS_CENTERED,
        width,
        height,
        SDL_WINDOW_SHOWN
    );
    if (app->window == NULL) {
        fprintf(stderr, "SDL_CreateWindow failed: %s\n", SDL_GetError());
        return false;
    }

    app->renderer = SDL_CreateRenderer(
        app->window,
        -1,
        SDL_RENDERER_ACCELERATED | SDL_RENDERER_PRESENTVSYNC
    );

    if (app->renderer == NULL) {
        // Xvfb often has no hardware acceleration; software fallback is required.
        app->renderer = SDL_CreateRenderer(app->window, -1, SDL_RENDERER_SOFTWARE);
    }

    if (app->renderer == NULL) {
        fprintf(stderr, "SDL_CreateRenderer failed: %s\n", SDL_GetError());
        window_destroy(app);
        return false;
    }

    return true;
}

void window_destroy(AppWindow *app) {
    if (app == NULL) {
        return;
    }

    if (app->renderer != NULL) {
        SDL_DestroyRenderer(app->renderer);
        app->renderer = NULL;
    }

    if (app->window != NULL) {
        SDL_DestroyWindow(app->window);
        app->window = NULL;
    }
}

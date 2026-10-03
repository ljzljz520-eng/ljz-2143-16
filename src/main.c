#include <stdbool.h>
#include <stdio.h>

#include <SDL2/SDL.h>
#include <SDL2/SDL_image.h>

#include "renderer.h"
#include "window.h"

#define WINDOW_TITLE "Visual Window App"
#define WINDOW_WIDTH 1280
#define WINDOW_HEIGHT 720
#define BACKGROUND_IMAGE_PATH "assets/background.png"

static bool init_sdl(void) {
    if (SDL_Init(SDL_INIT_VIDEO) != 0) {
        fprintf(stderr, "SDL_Init failed: %s\n", SDL_GetError());
        return false;
    }

    int img_flags = IMG_INIT_PNG | IMG_INIT_JPG;
    int initted = IMG_Init(img_flags);
    if ((initted & img_flags) == 0) {
        fprintf(stderr, "IMG_Init failed: %s\n", IMG_GetError());
        SDL_Quit();
        return false;
    }

    return true;
}

static void shutdown_sdl(void) {
    IMG_Quit();
    SDL_Quit();
}

int main(void) {
    if (!init_sdl()) {
        return 1;
    }

    AppWindow app = {0};
    if (!window_init(&app, WINDOW_TITLE, WINDOW_WIDTH, WINDOW_HEIGHT)) {
        shutdown_sdl();
        return 1;
    }

    SceneRenderer scene = {0};
    if (!renderer_load_background(&scene, app.renderer, BACKGROUND_IMAGE_PATH)) {
        window_destroy(&app);
        shutdown_sdl();
        return 1;
    }

    bool running = true;
    while (running) {
        SDL_Event event;
        while (SDL_PollEvent(&event) == 1) {
            if (event.type == SDL_QUIT) {
                running = false;
            } else if (event.type == SDL_WINDOWEVENT &&
                       event.window.event == SDL_WINDOWEVENT_CLOSE) {
                running = false;
            }
        }

        renderer_draw_background(&scene, app.renderer, app.width, app.height);
        SDL_Delay(16);
    }

    renderer_destroy(&scene);
    window_destroy(&app);
    shutdown_sdl();
    return 0;
}

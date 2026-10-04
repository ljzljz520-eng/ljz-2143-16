/*
 * device.c — 背景窗口"设备端"终端渲染器（headless 模型）
 *
 * 它与 src/ 下真实的 SDL2 窗口程序共享同一个场景模型：
 *   scene = {image 纹理, zoom 缩放, title 标题, darken 暗化}
 *
 * 关键语义（对应事务设计文档）：
 *   - PREPARE：在暂存区完整预备资源（读盘+解码校验+字段能力校验），全部成功才暂存；
 *     任何字段不支持 -> 整版拒绝，前台旧版不动，返回逐字段能力差异。
 *   - ACTIVATE：单次互斥区内"指针交换"完成整版呈现（原子呈现）；重复投递同一 plan 幂等。
 *   - PREVIEW：仅在当前编辑会话生效的临时纹理；PREVIEW_CANCEL 必须能撤销"下载慢于取消"
 *     的纹理（generation 栅栏：过期结果一律丢弃）。
 *
 * 传输协议：stdin/stdout 每行一个命令/响应，字段 key=value，以 TAB 分隔。
 * value 中的 TAB/NEWLINE 已做转义。
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <unistd.h>

/* ---------------------------- 能力与常量 ---------------------------- */

#define MAX_TITLE 512
#define MAX_PATH 4096
#define MAX_FIELD_ERR 1024
#define MAX_EVENTS 64
#define MAX_APPLIED 8
#define MAX_LINE 65536
#define MAX_IMAGE_BYTES (8u * 1024u * 1024u)
#define MAX_ZOOM_DEVICE 200          /* 本设备只支持 zoom 25..200 (%) */
#define MIN_ZOOM_DEVICE 25
#define MAX_DARKEN_DEVICE 90         /* 暗化 0..90 (%) */

typedef struct {
    char     path[MAX_PATH];
    uint8_t  sha256[32];
    int      width;
    int      height;
    size_t   bytes;
    char     title[MAX_TITLE];
    int      zoom;
    int      darken;
    int      valid;                  /* 暂存内容是否齐备 */
} Scene;

typedef struct {
    char    plan[64];
    int     rev;
    char    at[32];
} AppliedRec;

typedef struct {
    int      state;                  /* 0 idle 1 loading 2 ready 3 canceled 4 failed */
    char     session[64];
    uint64_t gen;
    char     path[MAX_PATH];
    uint8_t  sha256[32];
    int      width;
    int      height;
    size_t   bytes;
    int      delay_ms;
    char     error[256];
} Preview;

/* ---------------------------- SHA-256 ---------------------------- */

typedef struct { uint32_t h[8]; uint64_t len; uint8_t buf[64]; size_t blen; } Sha256;
static const uint32_t K256[64] = {
0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2 };

#define ROTR(x,n) (((x)>>(n))|((x)<<(32-(n))))

static void sha256_compress(Sha256 *s, const uint8_t *p) {
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[i*4]<<24 | (uint32_t)p[i*4+1]<<16 |
               (uint32_t)p[i*4+2]<<8 | (uint32_t)p[i*4+3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ROTR(w[i-15],7) ^ ROTR(w[i-15],18) ^ (w[i-15]>>3);
        uint32_t s1 = ROTR(w[i-2],17) ^ ROTR(w[i-2],19) ^ (w[i-2]>>10);
        w[i] = w[i-16] + s0 + w[i-7] + s1;
    }
    uint32_t a=s->h[0],b=s->h[1],c=s->h[2],d=s->h[3],e=s->h[4],
             f=s->h[5],g=s->h[6],hh=s->h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1=ROTR(e,6)^ROTR(e,11)^ROTR(e,25), ch=(e&f)^((~e)&g);
        uint32_t t1=hh+S1+ch+K256[i]+w[i];
        uint32_t S0=ROTR(a,2)^ROTR(a,13)^ROTR(a,22), maj=(a&b)^(a&c)^(b&c);
        uint32_t t2=S0+maj;
        hh=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
    }
    s->h[0]+=a; s->h[1]+=b; s->h[2]+=c; s->h[3]+=d;
    s->h[4]+=e; s->h[5]+=f; s->h[6]+=g; s->h[7]+=hh;
}
static void sha256_init(Sha256 *s) {
    static const uint32_t iv[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,
        0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    memcpy(s->h, iv, sizeof(iv)); s->len = 0; s->blen = 0;
}
static void sha256_update(Sha256 *s, const uint8_t *p, size_t n) {
    s->len += n;
    while (n) {
        size_t take = 64 - s->blen;
        if (take > n) take = n;
        memcpy(s->buf + s->blen, p, take);
        s->blen += take; p += take; n -= take;
        if (s->blen == 64) { sha256_compress(s, s->buf); s->blen = 0; }
    }
}
static void sha256_final(Sha256 *s, uint8_t out[32]) {
    uint64_t bits = s->len * 8;
    uint8_t pad = 0x80;
    sha256_update(s, &pad, 1);
    uint8_t z = 0;
    while (s->blen != 56) sha256_update(s, &z, 1);
    uint8_t lb[8];
    for (int i = 0; i < 8; i++) lb[i] = (uint8_t)(bits >> (56 - i*8));
    sha256_update(s, lb, 8);
    for (int i = 0; i < 8; i++)
        for (int j = 0; j < 4; j++) out[i*4+j] = (uint8_t)(s->h[i] >> (24-j*8));
}
static int hash_file(const char *path, uint8_t out[32], size_t *nbytes) {
    FILE *fp = fopen(path, "rb");
    if (!fp) return -1;
    Sha256 s; sha256_init(&s);
    uint8_t buf[65536]; size_t n, total = 0;
    while ((n = fread(buf, 1, sizeof(buf), fp)) > 0) {
        sha256_update(&s, buf, n); total += n;
        if (total > MAX_IMAGE_BYTES) { fclose(fp); return -2; }
    }
    int err = ferror(fp);
    fclose(fp);
    if (err) return -1;
    sha256_final(&s, out);
    if (nbytes) *nbytes = total;
    return 0;
}
static int parse_hex32(const char *hex, uint8_t out[32]) {
    if (!hex || strlen(hex) != 64) return -1;
    for (int i = 0; i < 32; i++) {
        unsigned v;
        if (sscanf(hex + i*2, "%2x", &v) != 1) return -1;
        out[i] = (uint8_t)v;
    }
    return 0;
}

/* ---------------------------- 图片解码（真实最小 PNG/JPEG 校验） ---------------------------- */

static uint32_t be32(const uint8_t *p) {
    return (uint32_t)p[0]<<24 | (uint32_t)p[1]<<16 | (uint32_t)p[2]<<8 | p[3];
}
/* 返回 1=可解码且写入宽高, 0=不可解码 */
static int image_probe(const uint8_t *data, size_t n, int *w, int *h, const char **fmt) {
    if (n >= 24 && data[0]==0x89 && data[1]=='P' && data[2]=='N' && data[3]=='G'
        && data[4]==0x0d && data[5]==0x0a && data[6]==0x1a && data[7]==0x0a) {
        if (memcmp(data+12, "IHDR", 4) != 0) return 0;
        uint32_t iw = be32(data+16), ih = be32(data+20);
        if (iw == 0 || ih == 0 || iw > 32768 || ih > 32768) return 0;
        *w = (int)iw; *h = (int)ih; *fmt = "png"; return 1;
    }
    if (n >= 4 && data[0]==0xFF && data[1]==0xD8 && data[2]==0xFF) {
        size_t i = 2;
        while (i + 9 < n) {
            if (data[i] != 0xFF) return 0;
            uint8_t m = data[i+1];
            if (m == 0xD9) return 0;
            if (m == 0xD8 || (m >= 0xD0 && m <= 0xD7)) { i += 2; continue; }
            if (i + 4 >= n) return 0;
            uint16_t seg = (uint16_t)(data[i+2]<<8 | data[i+3]);
            if (seg < 2) return 0;
            if ((m >= 0xC0 && m <= 0xC3) || (m >= 0xC5 && m <= 0xC7) ||
                (m >= 0xC9 && m <= 0xCB) || (m >= 0xCD && m <= 0xCF)) {
                if (i + 9 >= n) return 0;
                uint16_t ih = (uint16_t)(data[i+5]<<8 | data[i+6]);
                uint16_t iw = (uint16_t)(data[i+7]<<8 | data[i+8]);
                if (!iw || !ih) return 0;
                *w = iw; *h = ih; *fmt = "jpeg"; return 1;
            }
            i += 2 + seg;
        }
        return 0;
    }
    return 0;
}

/* ---------------------------- 全局状态 ---------------------------- */

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  work_cv = PTHREAD_COND_INITIALIZER;
static Scene    front;                 /* 当前现场（front buffer）*/
static Scene    staging;               /* 预备区（PREPARE 成功后在这里，等待 ACTIVATE）*/
static int      staging_ready = 0;
static char     staging_plan[64] = {0};
static int      staging_rev = 0;
static AppliedRec applied[MAX_APPLIED];
static int      applied_n = 0;
static Preview  preview;
static int      preview_job_pending = 0;
static int      render_tick = 0;
static char     events[MAX_EVENTS][256];
static int      events_head = 0, events_n = 0;
static int      running = 1;
static int      device_started = 0;

#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wformat-truncation"
static void nowstr(char *buf, size_t n) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    time_t t = (time_t)ts.tv_sec;
    struct tm tmv;
    gmtime_r(&t, &tmv);
    int ms = (int)(ts.tv_nsec / 1000000);
    snprintf(buf, n, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
        tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday,
        tmv.tm_hour, tmv.tm_min, tmv.tm_sec, ms);
}
#pragma GCC diagnostic pop
static void ev_push(const char *fmt, ...) {
    char *slot = events[events_head];
    va_list ap; va_start(ap, fmt);
    int n = vsnprintf(slot, 256, fmt, ap);
    va_end(ap);
    if (n > 255) { slot[253]='.'; slot[254]='.'; slot[255]=0; }
    events_head = (events_head + 1) % MAX_EVENTS;
    if (events_n < MAX_EVENTS) events_n++;
}
static void hex(char *out, const uint8_t *d, int n) {
    for (int i = 0; i < n; i++) sprintf(out + i*2, "%02x", d[i]);
    out[n*2] = 0;
}

/* ---------------------------- 响应输出（转义 TAB/NL） ---------------------------- */

static void out_escape(char *dst, size_t n, const char *src) {
    size_t j = 0;
    for (size_t i = 0; src[i] && j + 2 < n; i++) {
        unsigned char c = (unsigned char)src[i];
        if (c == '\t') { dst[j++]='\\'; dst[j++]='t'; }
        else if (c == '\n' || c == '\r') { dst[j++]=' '; }
        else if (c == '\\') { dst[j++]='\\'; dst[j++]='\\'; }
        else dst[j++] = (char)c;
    }
    dst[j] = 0;
}
static void respond(char ok, const char *extra_fmt, ...) {
    char extra[2048];
    if (extra_fmt) {
        va_list ap; va_start(ap, extra_fmt);
        vsnprintf(extra, sizeof(extra), extra_fmt, ap);
        va_end(ap);
    } else extra[0] = 0;
    char ts[40]; nowstr(ts, sizeof(ts));
    fprintf(stdout, "RES ok=%s at=%s%s%s\n", ok ? "1" : "0", ts, extra_fmt ? "\t" : "", extra);
    fflush(stdout);
}

/* ---------------------------- 命令解析 ---------------------------- */

static const char *kv(const char *line, const char *key, char *out, size_t n) {
    size_t kl = strlen(key);
    out[0] = 0;
    for (const char *p = line; p && *p; ) {
        const char *tab = strchr(p, '\t');
        size_t seg = tab ? (size_t)(tab - p) : strlen(p);
        if (seg > kl + 1 && p[kl] == '=' && strncmp(p, key, kl) == 0) {
            size_t v = seg - kl - 1;
            if (v >= n) v = n - 1;
            memcpy(out, p + kl + 1, v); out[v] = 0;
            return out;
        }
        if (!tab) break;
        p = tab + 1;
    }
    return NULL;
}
static int kv_int(const char *line, const char *key, int def) {
    char v[64];
    if (!kv(line, key, v, sizeof(v)) || !*v) return def;
    return atoi(v);
}
static void unescape(char *s) {
    char *r = s, *w = s;
    while (*r) {
        if (*r == '\\' && r[1] == 't') { *w++ = '\t'; r += 2; }
        else if (*r == '\\' && r[1] == '\\') { *w++ = '\\'; r += 2; }
        else *w++ = *r++;
    }
    *w = 0;
}

/* ---------------------------- 纹理加载 ---------------------------- */

/* 模拟终端侧"下载+解码"纹理：实际从本地共享目录读文件并真实校验文件头。
 * 返回 NULL 表示不可解码。成功时填充 Scene 的图片字段。*/
static int load_texture(Scene *sc, const char *path, const char *expect_hex,
                        int delay_ms, char *err, size_t errn) {
    if (delay_ms > 0) usleep((useconds_t)delay_ms * 1000);
    FILE *fp = fopen(path, "rb");
    if (!fp) { snprintf(err, errn, "image_unreachable path=%s", path); return -1; }
    uint8_t *buf = NULL; size_t cap = 0, n = 0;
    int ch;
    while ((ch = fgetc(fp)) != EOF) {
        if (n >= MAX_IMAGE_BYTES) { fclose(fp); free(buf);
            snprintf(err, errn, "image_too_large"); return -1; }
        if (n == cap) { cap = cap ? cap * 2 : 65536; uint8_t *nb = realloc(buf, cap);
            if (!nb) { fclose(fp); free(buf); snprintf(err, errn, "oom"); return -1; } buf = nb; }
        buf[n++] = (uint8_t)ch;
    }
    fclose(fp);
    uint8_t calc[32];
    Sha256 st; sha256_init(&st); sha256_update(&st, buf, n); sha256_final(&st, calc);
    if (expect_hex && *expect_hex) {
        uint8_t exp[32];
        if (parse_hex32(expect_hex, exp) != 0 || memcmp(calc, exp, 32) != 0) {
            free(buf); snprintf(err, errn, "checksum_mismatch"); return -1;
        }
    }
    const char *fmt = NULL; int w = 0, h = 0;
    if (!image_probe(buf, n, &w, &h, &fmt)) {
        free(buf); snprintf(err, errn, "image_not_decodable"); return -1;
    }
    free(buf);
    size_t bytes = 0;
    if (hash_file(path, sc->sha256, &bytes) != 0) {
        snprintf(err, errn, "hash_failed"); return -1;
    }
    snprintf(sc->path, sizeof(sc->path), "%s", path);
    sc->width = w; sc->height = h; sc->bytes = bytes;
    return 0;
}

/* ---------------------------- 预览加载线程 ---------------------------- */

static void *loader_main(void *arg) {
    (void)arg;
    pthread_mutex_lock(&lock);
    while (running) {
        while (running && !preview_job_pending)
            pthread_cond_wait(&work_cv, &lock);
        if (!running) break;
        preview_job_pending = 0;

        /* 拍下任务参数，在锁外执行慢速 IO */
        char path[MAX_PATH]; char expect[65]; int delay = preview.delay_ms;
        uint64_t gen = preview.gen; char session[64];
        snprintf(path, sizeof(path), "%s", preview.path);
        hex(expect, preview.sha256, 32);
        snprintf(session, sizeof(session), "%s", preview.session);
        preview.state = 1;
        pthread_mutex_unlock(&lock);

        Scene tmp = {0};
        char err[256] = {0};
        int rc = load_texture(&tmp, path, expect, delay, err, sizeof(err));

        pthread_mutex_lock(&lock);
        /* generation 栅栏：下载期间已被 CANCEL / 新 BEGIN 取代 -> 结果丢弃，
         * 临时纹理绝不落到前台，绝不污染正式版本。*/
        if (preview.gen != gen || strcmp(preview.session, session) != 0) {
            ev_push("preview job %s#%llu superseded, texture discarded",
                    session, (unsigned long long)gen);
            continue;
        }
        if (preview.state == 3) {   /* 已取消（典型：下载慢于取消）*/
            ev_push("preview job %s#%llu arrived after cancel, texture discarded",
                    session, (unsigned long long)gen);
            continue;
        }
        if (rc != 0) {
            preview.state = 4;
            snprintf(preview.error, sizeof(preview.error), "%s", err);
            ev_push("preview %s#%llu failed: %s", session, (unsigned long long)gen, err);
        } else {
            preview.state = 2;
            preview.bytes = tmp.bytes; preview.width = tmp.width; preview.height = tmp.height;
            memcpy(preview.sha256, tmp.sha256, 32);
            snprintf(preview.path, sizeof(preview.path), "%s", tmp.path);
            ev_push("preview %s#%llu ready %dx%d (session overlay only)",
                    session, (unsigned long long)gen, tmp.width, tmp.height);
        }
    }
    pthread_mutex_unlock(&lock);
    return NULL;
}

/* ---------------------------- 渲染线程（终端帧） ---------------------------- */

static void *render_main(void *arg) {
    (void)arg;
    char last_title[MAX_TITLE] = {0};
    int last_preview_state = -1, last_pgen = 0;
    for (;;) {
        /* 分段睡眠，保证关闭信号在 ~20ms 内被观察到（不使用 pthread_cancel）*/
        for (int i = 0; i < 10; i++) {
            usleep(20 * 1000);
            pthread_mutex_lock(&lock);
            int stop = !running;
            pthread_mutex_unlock(&lock);
            if (stop) return NULL;
        }
        pthread_mutex_lock(&lock);
        if (!running) { pthread_mutex_unlock(&lock); break; }
        render_tick++;
        char title[MAX_TITLE] = {0}; int zoom = 0, darken = 0;
        int pw = 0, ph = 0, pv = preview.state;
        char psess[64] = {0}; uint64_t pgen = preview.gen;
        if (front.valid) {
            snprintf(title, sizeof(title), "%s", front.title);
            zoom = front.zoom; darken = front.darken;
            pw = front.width; ph = front.height;
            if (pv == 2) snprintf(psess, sizeof(psess), "%s", preview.session);
        }
        if (front.valid && render_tick % 25 == 0) {
            fprintf(stderr, "[render] live: title='%s' zoom=%d%% darken=%d%% img=%dx%d%s\n",
                    title, zoom, darken, pw, ph,
                    pv == 2 ? " +PREVIEW-OVERLAY(session-only)" : "");
        }
        if (pv == 2 && (last_preview_state != 2 || (int)pgen != last_pgen))
            fprintf(stderr, "[render] session %s preview texture shown (temp overlay)\n", psess);
        if (last_preview_state == 2 && pv != 2)
            fprintf(stderr, "[render] preview overlay revoked, live config untouched\n");
        snprintf(last_title, sizeof(last_title), "%s", title);
        last_preview_state = pv; last_pgen = (int)pgen;
        pthread_mutex_unlock(&lock);
    }
    return NULL;
}

/* ---------------------------- 命令处理 ---------------------------- */

static int applied_has(const char *plan) {
    for (int i = 0; i < applied_n; i++)
        if (strcmp(applied[i].plan, plan) == 0) return 1;
    return 0;
}
static void applied_add(const char *plan, int rev, const char *at) {
    if (applied_has(plan)) return;
    AppliedRec *r = &applied[applied_n % MAX_APPLIED];
    snprintf(r->plan, sizeof(r->plan), "%s", plan);
    r->rev = rev; snprintf(r->at, sizeof(r->at), "%s", at);
    if (applied_n < MAX_APPLIED) applied_n++;
}

static void cmd_prepare(const char *line) {
    char plan[64], path[MAX_PATH], sha[65], title[MAX_TITLE];
    kv(line, "plan", plan, sizeof(plan));
    kv(line, "path", path, sizeof(path));
    kv(line, "sha256", sha, sizeof(sha));
    kv(line, "title", title, sizeof(title)); unescape(title);
    int rev = kv_int(line, "rev", 0), zoom = kv_int(line, "zoom", -1),
        darken = kv_int(line, "darken", -1);
    pthread_mutex_lock(&lock);

    /* 幂等：同 plan 已在暂存/已应用 */
    if (staging_ready && strcmp(staging_plan, plan) == 0) {
        char hx[65]; hex(hx, staging.sha256, 32);
        char te[MAX_TITLE]; out_escape(te, sizeof(te), staging.title);
        pthread_mutex_unlock(&lock);
        respond(1, "stage=already plan=%s rev=%d zoom=%d darken=%d w=%d h=%d sha256=%s",
                plan, staging_rev, staging.zoom, staging.darken, staging.width,
                staging.height, hx);
        return;
    }

    /* 逐字段能力差异收集；任一不通过 -> 整版拒绝，不动旧版 */
    char diff[MAX_FIELD_ERR] = {0};
    if (zoom < MIN_ZOOM_DEVICE || zoom > MAX_ZOOM_DEVICE)
        snprintf(diff + strlen(diff), sizeof(diff)-strlen(diff),
                 "field=zoom code=UNSUPPORTED_RANGE requested=%d supported=%d..%d unit=%%; ",
                 zoom, MIN_ZOOM_DEVICE, MAX_ZOOM_DEVICE);
    if (darken < 0 || darken > MAX_DARKEN_DEVICE)
        snprintf(diff + strlen(diff), sizeof(diff)-strlen(diff),
                 "field=darken code=UNSUPPORTED_RANGE requested=%d supported=0..%d unit=%%; ",
                 darken, MAX_DARKEN_DEVICE);
    if (!title[0] || (int)strlen(title) > 200)
        snprintf(diff + strlen(diff), sizeof(diff)-strlen(diff),
                 "field=title code=INVALID requested_len=%d supported=1..200; ",
                 (int)strlen(title));
    pthread_mutex_unlock(&lock);

    /* 慢速 IO 在锁外 */
    Scene cand = {0};
    char imgerr[256] = {0};
    if (load_texture(&cand, path, sha, 0, imgerr, sizeof(imgerr)) != 0) {
        snprintf(diff + strlen(diff), sizeof(diff)-strlen(diff),
                 "field=image code=%s path_len=%d; ", imgerr, (int)strlen(path));
    } else {
        snprintf(cand.title, sizeof(cand.title), "%s", title);
        cand.zoom = zoom; cand.darken = darken; cand.valid = 1;
    }

    pthread_mutex_lock(&lock);
    if (diff[0] || !cand.valid) {
        if (!diff[0])
            snprintf(diff, sizeof(diff),
                     "field=image code=UNREACHABLE path_len=%d; ", (int)strlen(path));
        ev_push("PREPARE %s rejected (old live version kept): %s", plan, diff);
        pthread_mutex_unlock(&lock);
        respond(0, "err=CAPABILITY_DIFF\tchanges=\"%s\"", diff);
        return;
    }
    staging = cand;
    staging_ready = 1;
    snprintf(staging_plan, sizeof(staging_plan), "%s", plan);
    staging_rev = rev;
    char hx[65]; hex(hx, staging.sha256, 32);
    char te[MAX_TITLE]; out_escape(te, sizeof(te), staging.title);
    ev_push("PREPARE %s staged rev=%d %dx%d zoom=%d darken=%d",
            plan, rev, staging.width, staging.height, zoom, darken);
    pthread_mutex_unlock(&lock);
    respond(1, "stage=staged plan=%s rev=%d zoom=%d darken=%d w=%d h=%d sha256=%s title=\"%s\"",
            plan, rev, zoom, darken, staging.width, staging.height, hx, te);
}

static void cmd_activate(const char *line) {
    char plan[64], at[32];
    kv(line, "plan", plan, sizeof(plan));
    int rev = kv_int(line, "rev", 0);
    kv(line, "at", at, sizeof(at)); if (!at[0]) nowstr(at, sizeof(at));
    pthread_mutex_lock(&lock);

    /* 幂等：同 plan 再来（确认丢失后的重试）-> 不重复切换，返回已应用 */
    if (applied_has(plan)) {
        char hx[65]; hex(hx, front.sha256, 32);
        char te[MAX_TITLE]; out_escape(te, sizeof(te), front.title);
        ev_push("ACTIVATE %s idempotent replay, live unchanged", plan);
        pthread_mutex_unlock(&lock);
        respond(1, "stage=already-active plan=%s rev=%d zoom=%d darken=%d w=%d h=%d sha256=%s title=\"%s\" applies=1",
                plan, rev, front.zoom, front.darken, front.width, front.height, hx, te);
        return;
    }
    if (!staging_ready || strcmp(staging_plan, plan) != 0) {
        ev_push("ACTIVATE %s failed: no staged candidate (old live kept)", plan);
        pthread_mutex_unlock(&lock);
        respond(0, "err=NOT_PREPARED plan=%s", plan);
        return;
    }
    /* 原子呈现点：锁内一次性整版指针交换，无中间态、无字段撕裂 */
    front = staging;
    memset(&staging, 0, sizeof(staging));
    staging_ready = 0; staging_plan[0] = 0;
    applied_add(plan, rev, at);
    char hx[65]; hex(hx, front.sha256, 32);
    char te[MAX_TITLE]; out_escape(te, sizeof(te), front.title);
    ev_push("ACTIVATE %s atomic swap -> rev=%d live %dx%d zoom=%d darken=%d",
            plan, rev, front.width, front.height, front.zoom, front.darken);
    pthread_mutex_unlock(&lock);
    respond(1, "stage=active plan=%s rev=%d zoom=%d darken=%d w=%d h=%d sha256=%s title=\"%s\" applies=1",
            plan, rev, front.zoom, front.darken, front.width, front.height, hx, te);
}

static void cmd_discard(const char *line) {
    char plan[64]; kv(line, "plan", plan, sizeof(plan));
    pthread_mutex_lock(&lock);
    if (staging_ready && (!plan[0] || strcmp(staging_plan, plan) == 0)) {
        memset(&staging, 0, sizeof(staging));
        staging_ready = 0; staging_plan[0] = 0;
        ev_push("DISCARD %s staging released, live untouched", plan);
        pthread_mutex_unlock(&lock);
        respond(1, "stage=discarded");
    } else {
        pthread_mutex_unlock(&lock);
        respond(1, "stage=nothing");
    }
}

static void cmd_preview_begin(const char *line) {
    char session[64], path[MAX_PATH], sha[65];
    kv(line, "session", session, sizeof(session));
    kv(line, "path", path, sizeof(path));
    kv(line, "sha256", sha, sizeof(sha));
    int delay = kv_int(line, "delay_ms", 0);
    pthread_mutex_lock(&lock);
    preview.gen++;                       /* 旧任务立刻过期（被新 begin 取代）*/
    snprintf(preview.session, sizeof(preview.session), "%s", session);
    snprintf(preview.path, sizeof(preview.path), "%s", path);
    if (parse_hex32(sha, preview.sha256) != 0) memset(preview.sha256, 0, 32);
    preview.delay_ms = delay;
    preview.state = 1; preview.error[0] = 0;
    preview.width = preview.height = 0; preview.bytes = 0;
    preview_job_pending = 1;
    pthread_cond_signal(&work_cv);
    ev_push("PREVIEW_BEGIN %s#%llu delay=%dms (temp overlay, not saved)",
            session, (unsigned long long)preview.gen, delay);
    uint64_t gen = preview.gen;
    pthread_mutex_unlock(&lock);
    respond(1, "preview=loading session=%s gen=%llu", session, (unsigned long long)gen);
}

static void cmd_preview_cancel(const char *line) {
    char session[64]; kv(line, "session", session, sizeof(session));
    pthread_mutex_lock(&lock);
    int had = preview.state != 0 && strcmp(preview.session, session) == 0;
    uint64_t gen = preview.gen;
    if (had) {
        preview.state = 3;             /* 若加载线程仍在慢速下载，完成后看到 3 -> 丢弃 */
        preview_job_pending = 0;
        ev_push("PREVIEW_CANCEL %s#%llu temp texture revoked (incl. slow in-flight download)",
                session, (unsigned long long)gen);
    }
    pthread_mutex_unlock(&lock);
    respond(1, "preview=canceled session=%s gen=%llu had=%d", session, (unsigned long long)gen, had);
}

static void cmd_status(void) {
    pthread_mutex_lock(&lock);
    char hx[65] = {0}, te[MAX_TITLE] = {0};
    if (front.valid) { hex(hx, front.sha256, 32); out_escape(te, sizeof(te), front.title); }
    char pv[128] = {0};
    if (preview.state) {
        const char *st[] = {"idle","loading","ready","canceled","failed"};
        char ph[65] = {0}; if (preview.state == 2) hex(ph, preview.sha256, 32);
        char pe[256] = {0}; out_escape(pe, sizeof(pe), preview.error);
        snprintf(pv, sizeof(pv), "pstate=%s psession=%s pgen=%llu pw=%d ph=%d perr=\"%s\" psha256=%s",
                 st[preview.state], preview.session, (unsigned long long)preview.gen,
                 preview.width, preview.height, pe, ph);
    } else snprintf(pv, sizeof(pv), "pstate=idle");
    char ap[1024] = {0};
    for (int i = 0; i < applied_n; i++) {
        char one[128];
        snprintf(one, sizeof(one), "%.56s:r%d%s", applied[i].plan, applied[i].rev,
                 i+1 < applied_n ? "," : "");
        strncat(ap, one, sizeof(ap)-strlen(ap)-1);
    }
    int sr = staging_ready;
    pthread_mutex_unlock(&lock);
    respond(1, "live=%d live_w=%d live_h=%d live_zoom=%d live_darken=%d live_title=\"%s\" live_sha256=%s staging=%s %s applied=\"%s\"",
            front.valid, front.width, front.height, front.zoom, front.darken, te, hx,
            sr ? staging_plan : "-", pv, ap);
}

static void cmd_events(void) {
    pthread_mutex_lock(&lock);
    /* 先输出全部 EVT，最后输出 RES（客户端以 RES 行作为一次请求的结束标记）*/
    int n = events_n;
    for (int i = 0; i < events_n; i++) {
        int idx = (events_head - events_n + i + MAX_EVENTS * 2) % MAX_EVENTS;
        char esc[300]; out_escape(esc, sizeof(esc), events[idx]);
        fprintf(stdout, "EVT %s\n", esc);
    }
    events_n = 0; events_head = 0;
    pthread_mutex_unlock(&lock);
    respond(1, "count=%d", n);
}

/* ---------------------------- main ---------------------------- */

int main(int argc, char **argv) {
    const char *seed_path = NULL, *seed_sha = NULL, *seed_title = "初始背景";
    int seed_zoom = 100, seed_darken = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--seed") && i+1 < argc) { seed_path = argv[++i]; continue; }
        if (!strcmp(argv[i], "--seed-sha256") && i+1 < argc) { seed_sha = argv[++i]; continue; }
        if (!strcmp(argv[i], "--seed-title") && i+1 < argc) { seed_title = argv[++i]; continue; }
        if (!strcmp(argv[i], "--seed-zoom") && i+1 < argc) { seed_zoom = atoi(argv[++i]); continue; }
        if (!strcmp(argv[i], "--seed-darken") && i+1 < argc) { seed_darken = atoi(argv[++i]); continue; }
        if (argv[i][0] == '-' && argv[i][1] == '-')
            fprintf(stderr, "[device] unknown argument: %s\n", argv[i]);
    }
    memset(&front, 0, sizeof(front)); memset(&staging, 0, sizeof(staging));
    memset(&preview, 0, sizeof(preview));
    if (seed_path) {
        char err[256];
        if (load_texture(&front, seed_path, seed_sha, 0, err, sizeof(err)) == 0) {
            snprintf(front.title, sizeof(front.title), "%s", seed_title);
            front.zoom = seed_zoom; front.darken = seed_darken; front.valid = 1;
        } else {
            fprintf(stderr, "[device] seed load failed: %s (starting with blank live)\n", err);
        }
    }
    pthread_t loader, renderer;
    pthread_create(&loader, NULL, loader_main, NULL);
    pthread_create(&renderer, NULL, render_main, NULL);
    device_started = 1;
    fprintf(stderr, "[device] ready: zoom %d..%d%%, darken 0..%d%%, PNG/JPEG decode\n",
            MIN_ZOOM_DEVICE, MAX_ZOOM_DEVICE, MAX_DARKEN_DEVICE);

    static char line[MAX_LINE];
    while (running && fgets(line, sizeof(line), stdin)) {
        size_t l = strlen(line);
        if (l && line[l-1] == '\n') line[--l] = 0;
        if (!l) continue;
        if (!strncmp(line, "HELLO", 5)) {
            respond(1, "service=bg-window-device ver=1 zoom_min=%d zoom_max=%d darken_max=%d formats=png,jpeg",
                    MIN_ZOOM_DEVICE, MAX_ZOOM_DEVICE, MAX_DARKEN_DEVICE);
        } else if (!strncmp(line, "PREPARE", 7)) cmd_prepare(line);
        else if (!strncmp(line, "ACTIVATE", 8)) cmd_activate(line);
        else if (!strncmp(line, "DISCARD", 7)) cmd_discard(line);
        else if (!strncmp(line, "PREVIEW_BEGIN", 13)) cmd_preview_begin(line);
        else if (!strncmp(line, "PREVIEW_CANCEL", 14)) cmd_preview_cancel(line);
        else if (!strncmp(line, "STATUS", 6)) cmd_status();
        else if (!strncmp(line, "EVENTS", 6)) cmd_events();
        else if (!strncmp(line, "SHUTDOWN", 8)) {
            respond(1, "bye=1");
            break;
        } else respond(0, "err=UNKNOWN_CMD");
    }
    pthread_mutex_lock(&lock); running = 0;
    pthread_cond_broadcast(&work_cv);
    pthread_mutex_unlock(&lock);
    pthread_join(loader, NULL);
    pthread_join(renderer, NULL);
    return 0;
}

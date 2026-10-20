/* Mock der PicoQuant-Bibliothek HH500Lib (HydraHarp 500) für Tests ohne Hardware.
 * Gleiche Funktionsnamen und Signaturen wie im HH500Lib-Handbuch (Linux v1.0.0.0, Kap. 7.2); T2-Records wie im
 * HydraHarp-500-Handbuch Kap. 6.3.2 (6 bit Kanal, 25 bit Zeitmarke, Überlauf-Records). Die Fehlercodes sind
 * eigene Werte (die echten stehen in errorcodes.h der Bibliothek) – getestet wird nur das Verhalten „< 0 = Fehler“
 * und die Übersetzung mit HH500_GetErrorString. Kein Code von PicoQuant.
 *
 * Umgebungsvariablen: MOCK_HH500_NODEV=1 (kein Gerät), MOCK_HERALD_RATE (1/s), MOCK_HH500_FIFOFULL_MS (FIFO-Überlauf
 * nach … ms), MOCK_HH500_SYSERROR=1 (Initialize scheitert), MOCK_HH500_BUSY=1 (Gerät von anderer Software
 * belegt: OpenDevice → ERROR_DEVICE_BUSY). */
#include <stdio.h>
#include <string.h>
#include "common.h"

#define E_OPEN -1
#define E_NOTINIT -2
#define E_INVARG -3
#define E_WRONGMODE -4
#define E_RUNNING -5
#define E_WRONG_TRGMODE -6
#define E_INIT -7
#define E_BUSY -8
#define TTREADMAX 1048576
#define WRAP 33554432ull
#define NCH 4

typedef struct {
    int open, init, mode, ref, running, sync_en, sync_div, sync_trg, ch_en[NCH], ch_trg[NCH], mk_en[4];
    int sync_lvl, ch_lvl[NCH], sync_off, ch_off[NCH], meas_ctrl, trigout;
    long long tacq_ms; double t0; uint64_t gen_ps, ofl_period;   /* erzeugt bis gen_ps; aktuelle Wrap-Periode */
    uint64_t mk_k; mock_herald_t her; double next_h;
    double cp[256]; int cp_h, cp_n;                                 /* ausstehende CH2-Kopien (Zeitordnung) */
    uint32_t *q; size_t qn, qcap;                                  /* noch nicht abgeholte Records */
    unsigned long long n_out;
} dev_t_;
static dev_t_ D[8];

static const char *err_text(int c) {
    switch (c) {
    case E_OPEN: return "ERROR_DEVICE_OPEN_FAIL (mock)";
    case E_NOTINIT: return "ERROR_DEVICE_NOT_INITIALIZED (mock)";
    case E_INVARG: return "ERROR_INVALID_ARGUMENT (mock)";
    case E_WRONGMODE: return "ERROR_INVALID_MODE (mock)";
    case E_RUNNING: return "ERROR_DEVICE_BUSY_MEASURING (mock)";
    case E_WRONG_TRGMODE: return "ERROR_WRONG_TRGMODE (mock)";
    case E_INIT: return "ERROR_INIT_FAILED (mock)";
    case E_BUSY: return "ERROR_DEVICE_BUSY (mock)";
    default: return "ERROR_UNKNOWN (mock)";
    }
}
int HH500_GetLibraryVersion(char *v) { strcpy(v, "1.0"); return 0; }
int HH500_GetErrorString(char *s, int c) { snprintf(s, 40, "%s", err_text(c)); return 0; }

#define DEV(d) if ((d) < 0 || (d) > 7) return E_INVARG; dev_t_ *X = &D[d];
#define INIT(d) DEV(d) if (!X->open || !X->init) return E_NOTINIT;
#define IDLE(d) INIT(d) if (X->running) return E_RUNNING;

int HH500_OpenDevice(int d, char *serial) {
    DEV(d)
    if (d != 0 || getenv("MOCK_HH500_NODEV")) return E_OPEN;
    if (getenv("MOCK_HH500_BUSY")) return E_BUSY;
    memset(X, 0, sizeof(*X)); X->open = 1; X->sync_div = 1; X->sync_en = 1;
    for (int i = 0; i < NCH; i++) X->ch_en[i] = 1;
    strcpy(serial, "1050042");
    return 0;
}
int HH500_CloseDevice(int d) { DEV(d) free(X->q); memset(X, 0, sizeof(*X)); return 0; }
int HH500_Initialize(int d, int mode, int ref) {
    DEV(d) if (!X->open) return E_OPEN;
    if (getenv("MOCK_HH500_SYSERROR")) return E_INIT;
    if (mode != 0 && mode != 2 && mode != 3 && mode != 8) return E_INVARG;
    if (ref < 0 || ref > 9) return E_INVARG;
    X->init = 1; X->mode = mode; X->ref = ref; return 0;
}
int HH500_GetHardwareInfo(int d, char *m, char *p, char *v) {
    INIT(d) strcpy(m, "HydraHarp 500 S (MOCK)"); strcpy(p, "930099"); strcpy(v, "1.0"); return 0;
}
int HH500_GetSerialNumber(int d, char *s) { INIT(d) strcpy(s, "1050042"); return 0; }
int HH500_GetNumOfInputChannels(int d, int *n) { INIT(d) *n = NCH; return 0; }
int HH500_GetBaseResolution(int d, double *r, int *b) { INIT(d) *r = 1.0; *b = 1; return 0; }
int HH500_SaveDebugDump(int d, char *path) {
    DEV(d) (void)X; FILE *f = fopen(path, "wb"); if (!f) return E_INVARG; fputs("HH500 MOCK DEBUG DUMP\n", f); fclose(f); return 0;
}
static int lvl_ok(int l) { return l >= -1200 && l <= 1200; }
static int off_ok(int o) { return o >= -99999 && o <= 99999; }
int HH500_SetSyncDiv(int d, int v) {
    IDLE(d) if (v != 1 && v != 2 && v != 4 && v != 8 && v != 16) return E_INVARG;
    if (X->mode == 2 && v != 1) return E_INVARG;     /* Mock: in T2 kein Teiler (Sync als Zeitstempelkanal) */
    X->sync_div = v; return 0;
}
int HH500_SetSyncTrgMode(int d, int m) { IDLE(d) if (m != 0 && m != 1) return E_INVARG; X->sync_trg = m; return 0; }
int HH500_SetSyncEdgeTrg(int d, int l, int e) {
    IDLE(d) if (X->sync_trg != 0) return E_WRONG_TRGMODE; if (!lvl_ok(l) || (e != 0 && e != 1)) return E_INVARG;
    X->sync_lvl = l; return 0;
}
int HH500_SetSyncCFD(int d, int l, int z) {
    IDLE(d) if (X->sync_trg != 1) return E_WRONG_TRGMODE; if (l > 0 || l < -1200 || z < -40 || z > 0) return E_INVARG;
    X->sync_lvl = l; return 0;
}
int HH500_SetSyncChannelOffset(int d, int v) { IDLE(d) if (!off_ok(v)) return E_INVARG; X->sync_off = v; return 0; }
int HH500_SetSyncChannelEnable(int d, int e) { IDLE(d) X->sync_en = e ? 1 : 0; return 0; }
#define CH(c) if ((c) < -1 || (c) >= NCH) return E_INVARG;
#define FORCH(c, stmt) for (int i = ((c) < 0 ? 0 : (c)); i < ((c) < 0 ? NCH : (c) + 1); i++) { stmt; }
int HH500_SetInputTrgMode(int d, int c, int m) { IDLE(d) CH(c) if (m != 0 && m != 1) return E_INVARG; FORCH(c, X->ch_trg[i] = m) return 0; }
int HH500_SetInputEdgeTrg(int d, int c, int l, int e) {
    IDLE(d) CH(c) if (!lvl_ok(l) || (e != 0 && e != 1)) return E_INVARG;
    FORCH(c, if (X->ch_trg[i] != 0) return E_WRONG_TRGMODE) FORCH(c, X->ch_lvl[i] = l) return 0;
}
int HH500_SetInputCFD(int d, int c, int l, int z) {
    IDLE(d) CH(c) if (l > 0 || l < -1200 || z < -40 || z > 0) return E_INVARG;
    FORCH(c, if (X->ch_trg[i] != 1) return E_WRONG_TRGMODE) FORCH(c, X->ch_lvl[i] = l) return 0;
}
int HH500_SetInputChannelOffset(int d, int c, int v) { IDLE(d) CH(c) if (!off_ok(v)) return E_INVARG; FORCH(c, X->ch_off[i] = v) return 0; }
int HH500_SetInputChannelEnable(int d, int c, int e) { IDLE(d) CH(c) FORCH(c, X->ch_en[i] = e ? 1 : 0) return 0; }
int HH500_SetMarkerEdges(int d, int a, int b, int c, int e) { IDLE(d) (void)a; (void)b; (void)c; (void)e; return 0; }
int HH500_SetMarkerEnable(int d, int a, int b, int c, int e) { IDLE(d) X->mk_en[0] = a; X->mk_en[1] = b; X->mk_en[2] = c; X->mk_en[3] = e; return 0; }
int HH500_SetTriggerOutput(int d, int p) { IDLE(d) if (p < 0 || p > 16777215) return E_INVARG; X->trigout = p; return 0; }
int HH500_SetMeasControl(int d, int m, int a, int b) { IDLE(d) (void)a; (void)b; if (m < 0 || m > 9) return E_INVARG; X->meas_ctrl = m; return 0; }
int HH500_StartMeas(int d, int tacq) {
    IDLE(d) if (X->mode != 2 && X->mode != 3) return E_WRONGMODE;
    if (tacq < 1 || tacq > 360000000) return E_INVARG;
    X->tacq_ms = tacq; X->t0 = mock_now_s(); X->running = 1; X->gen_ps = 0; X->ofl_period = 0; X->mk_k = 0;
    X->qn = 0; X->n_out = 0; X->cp_h = X->cp_n = 0; mock_herald_init(&X->her); X->next_h = mock_herald_next(&X->her);
    return 0;
}
int HH500_StopMeas(int d) { INIT(d) X->running = 0; return 0; }
static double elapsed_s(dev_t_ *X) {
    double e = mock_now_s() - X->t0;
    if (X->meas_ctrl == 0 && e > X->tacq_ms * 1e-3) e = X->tacq_ms * 1e-3;
    return e;
}
int HH500_CTCStatus(int d, int *s) {
    INIT(d) *s = (X->meas_ctrl == 0 && (!X->running || mock_now_s() - X->t0 >= X->tacq_ms * 1e-3)) ? 1 : 0; return 0;
}
int HH500_GetElapsedMeasTime(int d, double *e) { INIT(d) *e = 1e3 * elapsed_s(X); return 0; }

static void push(dev_t_ *X, uint32_t w) {
    if (X->qn == X->qcap) { X->qcap = X->qcap ? 2 * X->qcap : 4096; X->q = realloc(X->q, X->qcap * sizeof(uint32_t)); }
    X->q[X->qn++] = w;
}
static void emit(dev_t_ *X, uint64_t t_ps, int special, int chan) {
    uint64_t per = t_ps / WRAP;
    if (per > X->ofl_period) {                       /* Überlauf-Records: special=1, channel=63, timetag=Anzahl */
        uint64_t n = per - X->ofl_period;
        while (n) { uint64_t m = n > (WRAP - 1) ? (WRAP - 1) : n; push(X, (1u << 31) | (63u << 25) | (uint32_t)m); n -= m; }
        X->ofl_period = per;
    }
    push(X, ((uint32_t)special << 31) | ((uint32_t)chan << 25) | (uint32_t)(t_ps % WRAP));
}
static void generate(dev_t_ *X, uint64_t until_ps) {   /* Ereignisse streng in Zeitordnung bis until_ps */
    for (;;) {
        double tm = mock_marker_time(X->mk_k) + 15e-9 + X->sync_off * 1e-12;
        double th = X->next_h + X->ch_off[0] * 1e-12;
        double tc = X->cp_n ? X->cp[X->cp_h] : 1e300;
        int which = 0; double t = tm;
        if (th < t) { t = th; which = 1; }
        if (tc < t) { t = tc; which = 2; }
        uint64_t tps = (uint64_t)llround(t * 1e12);
        if (tps > until_ps) break;
        if (which == 0) {
            if (X->sync_en && !mock_marker_omitted(X->mk_k)) emit(X, tps, 1, 0);   /* Sync-Eingang (Kanal 0) */
            X->mk_k++;
        } else if (which == 1) {
            if (X->ch_en[0]) emit(X, tps, 0, 0);                                   /* CH1 Herald */
            if (X->ch_en[1] && X->cp_n < 256) {                                    /* CH2 Kopie ADQ32-Trigger */
                X->cp[(X->cp_h + X->cp_n) % 256] = X->next_h + 52.7e-9 + X->ch_off[1] * 1e-12; X->cp_n++;
            }
            X->next_h = mock_herald_next(&X->her);
        } else {
            emit(X, tps, 0, 1);
            X->cp_h = (X->cp_h + 1) % 256; X->cp_n--;
        }
    }
    X->gen_ps = until_ps;
}
int HH500_ReadFiFo(int d, unsigned int *buf, int *n) {
    INIT(d) *n = 0;
    double ff = env_d("MOCK_HH500_FIFOFULL_MS", -1);
    if (X->running || X->qn) {
        mock_sleep_s(0.01);                           /* „kehrt typischerweise nach ≈ 10 ms zurück“ */
        if (X->running) generate(X, (uint64_t)llround(elapsed_s(X) * 1e12));
        size_t k = X->qn < TTREADMAX ? X->qn : TTREADMAX;
        if (k) {
            memcpy(buf, X->q, k * sizeof(uint32_t));
            memmove(X->q, X->q + k, (X->qn - k) * sizeof(uint32_t)); X->qn -= k;
        }
        *n = (int)k; X->n_out += k;
    }
    (void)ff;
    return 0;
}
int HH500_GetFlags(int d, int *f) {
    INIT(d) double ff = env_d("MOCK_HH500_FIFOFULL_MS", -1);
    *f = (X->running ? 0x20 : 0) | ((ff >= 0 && X->running && (mock_now_s() - X->t0) * 1e3 > ff) ? 0x2 : 0);
    return 0;
}
int HH500_GetWarnings(int d, int *w) { INIT(d) *w = 0; return 0; }
int HH500_GetWarningsText(int d, char *t, int w) { INIT(d) (void)w; t[0] = 0; return 0; }
int HH500_GetSyncRate(int d, int *r) { INIT(d) *r = X->sync_en ? 900 : 0; return 0; }
int HH500_GetAllCountRates(int d, int *s, int *c) {
    INIT(d) *s = X->sync_en ? 900 : 0;
    for (int i = 0; i < NCH; i++) c[i] = X->ch_en[i] && i < 2 ? (int)env_d("MOCK_HERALD_RATE", 1000) : 0;
    return 0;
}

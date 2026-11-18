/* Gemeinsame Ereignisquelle der Mock-Bibliotheken (HH500Lib, ADQAPI): beide Geräte sehen dieselben, deterministischen
 * Heralds (Poisson) und SYNC-Marker (1 kHz, pseudozufällige Auslassung 10 %), jeweils ab ihrem eigenen Messstart.
 * Damit lassen sich die beiden Mock-Datenströme wie echte synchronisieren. Kein Code der Hersteller. */
#ifndef HHD_MOCK_COMMON_H
#define HHD_MOCK_COMMON_H
#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#include <time.h>
#ifdef _WIN32
/* Windows (MinGW-w64, statisch gelinkt): Zeit und Warten über die Win32-API (ohne winpthread), Umgebungsvariablen
 * aus dem Prozess-Environment (GetEnvironmentVariableA) – getenv() der DLL-eigenen C-Laufzeit sähe Änderungen
 * aus Python (os.environ, monkeypatch.setenv) nach dem Prozessstart nicht. */
#include <windows.h>
static inline double mock_now_s(void) {
    LARGE_INTEGER f, c; QueryPerformanceFrequency(&f); QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)f.QuadPart;
}
#ifndef CREATE_WAITABLE_TIMER_HIGH_RESOLUTION
#define CREATE_WAITABLE_TIMER_HIGH_RESOLUTION 0x00000002
#endif
/* Sleep() hat ≈ 15,6 ms Auflösung; ReadFiFo soll wie das Gerät nach ≈ 10 ms zurückkehren → hochauflösender Timer
 * (Windows 10 1803+), sonst Sleep(). */
static inline void mock_sleep_s(double s) {
    if (s <= 0) return;
    HANDLE t = CreateWaitableTimerExW(NULL, NULL, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
    if (t) {
        LARGE_INTEGER due; due.QuadPart = -(LONGLONG)(s * 1e7);       /* relativ, 100-ns-Einheiten */
        if (SetWaitableTimer(t, &due, 0, NULL, NULL, FALSE)) { WaitForSingleObject(t, INFINITE); CloseHandle(t); return; }
        CloseHandle(t);
    }
    Sleep((DWORD)(s * 1000.0 + 0.5));
}
static inline const char *mock_getenv(const char *k) {
    static __thread char buf[512];
    DWORD n = GetEnvironmentVariableA(k, buf, (DWORD)sizeof buf);
    return (n > 0 && n < sizeof buf) ? buf : NULL;
}
#else
static inline double mock_now_s(void) {
    struct timespec ts; clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + 1e-9 * ts.tv_nsec;
}
static inline void mock_sleep_s(double s) {
    if (s <= 0) return;
    struct timespec ts; ts.tv_sec = (time_t)s; ts.tv_nsec = (long)((s - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
}
static inline const char *mock_getenv(const char *k) { return getenv(k); }
#endif
static inline uint64_t mock_xs(uint64_t *s) { uint64_t x = *s; x ^= x << 13; x ^= x >> 7; x ^= x << 17; return *s = x; }
static inline double mock_u01(uint64_t *s) { return ((mock_xs(s) >> 11) + 0.5) * (1.0 / 9007199254740992.0); }
static inline double mock_gauss(uint64_t *s) {
    double u = mock_u01(s), v = mock_u01(s);
    return sqrt(-2.0 * log(u)) * cos(6.283185307179586 * v);
}
static inline double env_d(const char *k, double d) { const char *v = mock_getenv(k); return v ? atof(v) : d; }

#define MOCK_F_MARKER 1000.0
static inline int mock_marker_omitted(uint64_t k) {          /* Muster für die Zuordnung (wie markers.p_omit) */
    uint64_t h = k * 0x9E3779B97F4A7C15ull; h ^= h >> 29; h *= 0xBF58476D1CE4E5B9ull; h ^= h >> 32;
    return (h % 10ull) == 0ull;
}
static inline double mock_marker_time(uint64_t k) { return (k + 0.5) / MOCK_F_MARKER; }

typedef struct { uint64_t s; double t; double rate; } mock_herald_t;
static inline void mock_herald_init(mock_herald_t *h) {
    h->s = 0x2545F4914F6CDD1Dull; h->t = 0.0; h->rate = env_d("MOCK_HERALD_RATE", 1000.0);
}
static inline double mock_herald_next(mock_herald_t *h) {      /* nächster Herald (s ab Messstart) */
    if (h->rate <= 0) return 1e300;
    h->t += -log(mock_u01(&h->s)) / h->rate;
    return h->t;
}
#endif
